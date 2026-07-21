#!/usr/bin/env python3
"""Refresh agent-model.yaml from live provider /v1/models endpoints.

For each provider in agent-model.yaml `providers:` (that you have a key for),
GET {base_url}/models, keep the text-generation models, auto-shorten the ids to
convenient shortcuts, and rewrite the matching harness's model list so `-m
<shortcut>` stays in sync with what the endpoint actually serves.

Usage:
    gather-models.py                 # all providers with a resolvable key
    gather-models.py orch_model      # one provider id
    gather-models.py opencode        # all providers for a harness
    gather-models.py codex           # refresh codex from the openai provider
    gather-models.py --dry-run ...   # show what would change, do not write

A provider with `source: codex_cache` reads codex's own OAuth-refreshed model
cache (~/.codex/models_cache.json) instead of a keyed /v1/models fetch — codex
authenticates via ChatGPT OAuth and has no platform API key. A provider with
`bare_model: true` (the codex case) stores the plain model id for `-m`;
everything else stores the provider-routed `<pid>/<model>` id opencode needs.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

# Non-text model ids to drop (embeddings, rerank, tts, image, routers, ...).
# The second line covers OpenAI-catalog non-chat families (dall-e, moderation,
# legacy completion, audio/realtime/transcribe, search-preview, computer-use,
# sora) so the codex menu gathered from api.openai.com stays chat-only.
_SKIP = re.compile(
    r"(embed|rerank|tts|voicedesign|whisper|image|stable-diffusion|wan2|"
    r"mini-lm|mpnet|^bge|^e5-|^gte-|^all-|^router:|reranker|"
    r"dall-e|moderation|davinci|babbage|realtime|audio|transcribe|"
    r"search-preview|computer-use|sora)",
    re.IGNORECASE,
)
_VENDOR_PREFIX = re.compile(r"^(openai-|anthropic-|alibaba-|nvidia-|meta-)")


def _find_yaml() -> str:
    for c in [
        os.environ.get("AGENT_MODEL_YAML"),
        os.path.join(os.environ["WORKSPACE_ROOT"], "agent-model.yaml")
        if os.environ.get("WORKSPACE_ROOT") else None,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent-model.yaml"),
    ]:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    raise SystemExit("agent-model.yaml not found (set AGENT_MODEL_YAML or WORKSPACE_ROOT)")


def _fetch_models(base_url: str, api_key: str) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return [m.get("id", "") for m in data.get("data", []) if m.get("id")]


def _read_codex_cache(path: str) -> list[tuple[str, str]]:
    """Return (slug, note) for user-listable models from a codex models_cache.json.

    codex authenticates via ChatGPT OAuth and refreshes this cache itself, so it
    is the key-free source of the codex model menu. Only entries with
    visibility 'list' are surfaced (hidden/deprecated/internal ones are skipped);
    order is preserved so the first listed model can seed a sensible default.
    """
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        data = json.load(fh)
    out: list[tuple[str, str]] = []
    for m in data.get("models", []):
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or m.get("id")
        if not slug or m.get("visibility") not in (None, "list"):
            continue
        name = m.get("display_name") or slug
        desc = (m.get("description") or "").strip()
        out.append((slug, f"{name} — {desc}" if desc else name))
    return out


def _shorten(model_id: str, taken: set[str]) -> str:
    short = _VENDOR_PREFIX.sub("", model_id)
    if short in taken or not short:
        short = model_id  # collision or empty -> keep full id
    return short


def main(argv: list[str]) -> int:
    import yaml

    dry = "--dry-run" in argv
    targets = [a for a in argv if a != "--dry-run"]

    path = _find_yaml()
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    providers = data.get("providers", {}) or {}
    harnesses = data.setdefault("harnesses", {})

    # Resolve which provider ids to gather.
    selected = {}
    for pid, pdef in providers.items():
        pdef = pdef or {}
        if not targets or pid in targets or pdef.get("harness") in targets:
            selected[pid] = pdef
    if not selected:
        print(f"no matching providers (known: {', '.join(providers)})", file=sys.stderr)
        return 2

    changed = False
    for pid, pdef in selected.items():
        harness = pdef.get("harness")
        if not harness:
            print(f"skip {pid}: missing harness", file=sys.stderr)
            continue
        source = str(pdef.get("source") or "http")

        # Collect (model_id, note) pairs from the provider's source, keeping the
        # order the source lists them (used to seed a sensible default).
        try:
            if source == "codex_cache":
                # codex is OAuth-authenticated — read its own cache, no key/URL.
                entries = _read_codex_cache(
                    pdef.get("cache_file") or "~/.codex/models_cache.json")
            else:
                base_url = pdef.get("base_url")
                key_env = pdef.get("api_key_env", "")
                key = os.environ.get(key_env, "")
                if not base_url:
                    print(f"skip {pid}: missing base_url", file=sys.stderr)
                    continue
                if not key:
                    print(f"skip {pid}: {key_env} not set in environment", file=sys.stderr)
                    continue
                ids = sorted(i for i in _fetch_models(base_url, key) if not _SKIP.search(i))
                entries = [(mid, f"{mid} via {pid}") for mid in ids]
        except Exception as exc:  # noqa: BLE001
            print(f"skip {pid}: gather failed ({exc})", file=sys.stderr)
            continue

        if not entries:
            print(f"skip {pid}: no models returned", file=sys.stderr)
            continue

        # Native-auth harnesses (codex) consume a bare model id via -m; the
        # opencode adapter wants the provider-routed `<pid>/<mid>` form.
        bare = bool(pdef.get("bare_model"))
        hz = harnesses.setdefault(harness, {"provider": "opensource", "models": {}})
        models = hz.setdefault("models", {})

        # Drop the entries this provider owns, keep everything else. Ownership is
        # the `provider:` marker (written below) OR, for legacy prefixed entries,
        # a `<pid>/` model prefix. Bare entries carry no prefix, so the marker is
        # what lets a re-run replace them cleanly.
        prefix = f"{pid}/"
        models = {k: v for k, v in models.items()
                  if (v or {}).get("provider") != pid
                  and not str((v or {}).get("model", "")).startswith(prefix)}

        taken = set(models.keys())
        added = []
        for mid, note in entries:
            short = _shorten(mid, taken)
            taken.add(short)
            model_str = mid if bare else f"{pid}/{mid}"
            models[short] = {"model": model_str, "note": note, "provider": pid}
            added.append(short)

        hz["models"] = dict(sorted(models.items()))
        # Keep the current default if it survived; else prefer this provider's
        # first source-listed model over an arbitrary alphabetical pick.
        if hz.get("default") not in hz["models"]:
            hz["default"] = added[0] if added else next(iter(hz["models"]), "")
        changed = True
        print(f"{pid} ({harness}): {len(added)} models -> {', '.join(added)}")

    if not changed:
        print("nothing gathered (no providers with keys reachable).", file=sys.stderr)
        return 1

    if dry:
        print("\n--dry-run: no file written.")
        return 0

    header = (
        "# agent-model.yaml — valid (harness x model) combinations.\n"
        "# The `opencode` and `codex` model lists are auto-generated by\n"
        "# gather-models.py from each provider's source (opencode: live provider\n"
        "# /v1/models; codex: its OAuth-refreshed ~/.codex model cache). Edit\n"
        "# `providers:` or re-run gather to refresh. codex stores bare model ids\n"
        "# (native auth at runtime); opencode stores `<pid>/<model>` ids.\n"
        "# claude uses native auth and is hand-maintained.\n"
        "# List all combos:  agent-launchers/resolve-model.py --list\n"
        "# -m goes BEFORE the runtime:  ./start-dev-worker.sh backend -m deepseek opencode\n\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False,
                       width=100, allow_unicode=True)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
