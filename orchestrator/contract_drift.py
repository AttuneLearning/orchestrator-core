"""Contract drift checker: read-only consistency verifier for contracts, routes, and frontend.

Pure diff core (no I/O, no DB imports) that cross-references the registry against
API routes, shared DTOs, and frontend endpoints, classifying drift as BLOCKING or
ADVISORY. Thin I/O loaders gather data from live sources (repository, filesystem).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from psycopg_pool import ConnectionPool

from . import repository
from .contract_lifecycle import normalize_path
from .config import Settings


# ============================================================================ #
# Pure diff core — no I/O, no DB, no filesystem imports                      #
# ============================================================================ #

_SATISFIED = frozenset({"agreed", "live"})
_DEAD = frozenset({"superseded", "deprecated", "retired", "rejected"})
# Auth tokens treated as "requires auth" — route scanner emits "jwt"; contracts
# may say "bearer"/"jwt"; "none"/"" == public.
_AUTHED = frozenset({"jwt", "bearer", "authenticated", "authorize"})


def _key(method: str, path: str) -> tuple[str, str]:
    """Normalize method and path for matching. Returns (METHOD, normalized_path)."""
    return (method.upper(), normalize_path(path))


def diff_contracts(
    contracts: list[dict],
    audit_routes: list[dict],
    fe_endpoints: list[dict],
    excluded: set[tuple[str, str]],
) -> list[dict]:
    """Cross-reference registry vs routes vs frontend and return typed findings.

    Every finding is a dict with EXACTLY these keys:
      {
        "category": str,      # one of the category slugs
        "severity": str,      # "blocking" | "advisory"
        "method":   str,      # HTTP method, or "" when FE ref is method-less
        "path":     str,      # the offending raw path
        "contract_id": int | None,  # the related registry contract id, else None
        "detail":   str,      # one-line human explanation
      }
    Ordering: blocking first, then advisory; stable within a severity by
    (category, method, path). Pure: no I/O, deterministic on inputs.
    """
    findings: list[dict] = []

    # Normalize the excluded set to handle both raw and normalized forms.
    # Pre-compute normalized exclusions so we check both forms.
    excluded_by_norm: dict[str, set[str]] = {}  # normalized_path -> set of methods
    for method, path in excluded:
        norm_path = normalize_path(path)
        if norm_path not in excluded_by_norm:
            excluded_by_norm[norm_path] = set()
        excluded_by_norm[norm_path].add(method.upper())

    def is_excluded(method: str | None, path: str) -> bool:
        """Check if a (method, path) is in the exclusion set (raw or normalized).

        For method-less FE refs, checks if any excluded route matches the path.
        For method-ful refs, checks exact method+path match.
        """
        norm_path = normalize_path(path)
        if not method:
            # Method-less: excluded if ANY excluded route matches this path
            return norm_path in excluded_by_norm
        else:
            # Method-ful: excluded if exact (method, path) is excluded
            method_upper = method.upper()
            if norm_path in excluded_by_norm and method_upper in excluded_by_norm[norm_path]:
                return True
            # Also check raw form
            return (method_upper, path) in excluded

    # Build index: contracts by _key
    contracts_by_key: dict[tuple[str, str], list[dict]] = {}
    for c in contracts:
        if is_excluded(c["method"], c["path"]):
            continue
        key = _key(c["method"], c["path"])
        if key not in contracts_by_key:
            contracts_by_key[key] = []
        contracts_by_key[key].append(c)

    # Build index: audit routes by _key
    routes_by_key: dict[tuple[str, str], list[dict]] = {}
    for r in audit_routes:
        if is_excluded(r["method"], r["path"]):
            continue
        key = _key(r["method"], r["path"])
        if key not in routes_by_key:
            routes_by_key[key] = []
        routes_by_key[key].append(r)

    # Build index: active contracts (status in _SATISFIED) by _key
    active_by_key: dict[tuple[str, str], list[dict]] = {}
    for c in contracts:
        if is_excluded(c["method"], c["path"]):
            continue
        if c.get("status") in _SATISFIED:
            key = _key(c["method"], c["path"])
            if key not in active_by_key:
                active_by_key[key] = []
            active_by_key[key].append(c)

    # -------- Category 1: unbacked_contract -------- #
    # Registry contract has NO audit route at its _key.
    for c in contracts:
        if is_excluded(c["method"], c["path"]):
            continue
        key = _key(c["method"], c["path"])
        if key not in routes_by_key:
            # No matching route; check severity
            severity = "blocking" if c.get("status") in _SATISFIED else "advisory"
            findings.append({
                "category": "unbacked_contract",
                "severity": severity,
                "method": c["method"],
                "path": c["path"],
                "contract_id": c.get("id"),
                "detail": f"contract {c.get('status')} but has no matching audit route",
            })

    # -------- Category 2: missing_contract -------- #
    # Audit route has NO active registry contract at its _key.
    for r in audit_routes:
        if is_excluded(r["method"], r["path"]):
            continue
        key = _key(r["method"], r["path"])
        if key not in active_by_key:
            findings.append({
                "category": "missing_contract",
                "severity": "advisory",
                "method": r["method"],
                "path": r["path"],
                "contract_id": None,
                "detail": "route is live but has no active contract in registry",
            })

    # -------- Category 3: auth_mismatch -------- #
    # Contract and audit route match on _key, contract is active,
    # but auth class differs.
    for key in contracts_by_key:
        if key not in routes_by_key:
            continue
        for c in contracts_by_key[key]:
            if c.get("status") not in _SATISFIED:
                continue
            for r in routes_by_key[key]:
                c_authed = (c.get("auth") or "").lower() in _AUTHED
                r_authed = (r.get("auth") or "").lower() in _AUTHED
                if c_authed != r_authed:
                    findings.append({
                        "category": "auth_mismatch",
                        "severity": "advisory",
                        "method": c["method"],
                        "path": c["path"],
                        "contract_id": c.get("id"),
                        "detail": f"auth mismatch: contract {c.get('auth')} vs route {r.get('auth')}",
                    })

    # -------- Category 4: path_normalization_drift -------- #
    # An active contract matches a route ONLY after normalization
    # while the RAW path strings differ.
    for key in active_by_key:
        if key not in routes_by_key:
            continue
        for c in active_by_key[key]:
            for r in routes_by_key[key]:
                # They have the same _key (method + normalized path) but may differ in raw path
                if c["path"] != r["path"]:
                    findings.append({
                        "category": "path_normalization_drift",
                        "severity": "advisory",
                        "method": c["method"],
                        "path": c["path"],
                        "contract_id": c.get("id"),
                        "detail": f"path params differ: contract {c['path']} vs route {r['path']}",
                    })

    # -------- Category 5a: fe_calls_dead_contract -------- #
    # A FE endpoint matches a registry contract whose status in _DEAD.
    # NOTE: deliberately NOT exclusion-filtered. _BULK_IMPORT_EXCLUDED marks
    # (method, path) pairs intentionally kept OUT of the registry; it says
    # nothing about contracts that DO exist in the registry as dead
    # (superseded/deprecated/retired/rejected). A FE call into a real dead
    # contract must be surfaced regardless of the bulk-import exclusion list,
    # otherwise the whole point of FE-calls-dead detection is defeated for
    # any dead contract whose (method, path) happens to coincide with an
    # excluded entry (e.g. the notifications family).
    for fe in fe_endpoints:
        # FE refs may be method-less; match on normalized path against any method
        fe_path_norm = normalize_path(fe["path"])
        fe_method = fe.get("method")

        for c in contracts:
            if c.get("status") not in _DEAD:
                continue
            # Check if FE matches this dead contract
            c_method = c["method"]
            c_path_norm = normalize_path(c["path"])

            # Match: if FE has method, must match exactly; if method-less, match by path only
            if fe_method:
                if fe_method.upper() == c_method and fe_path_norm == c_path_norm:
                    findings.append({
                        "category": "fe_calls_dead_contract",
                        "severity": "blocking",
                        "method": fe_method,
                        "path": fe["path"],
                        "contract_id": c.get("id"),
                        "detail": f"frontend calls {c.get('status')} contract #{c.get('id')}",
                    })
            else:
                # Method-less: match by path only
                if fe_path_norm == c_path_norm:
                    findings.append({
                        "category": "fe_calls_dead_contract",
                        "severity": "blocking",
                        "method": "",
                        "path": fe["path"],
                        "contract_id": c.get("id"),
                        "detail": f"frontend calls {c.get('status')} contract #{c.get('id')}",
                    })

    # -------- Category 5b: fe_unbacked_path -------- #
    # A FE endpoint has NO active registry contract AND no _DEAD match.
    for fe in fe_endpoints:
        if is_excluded(fe.get("method"), fe["path"]):
            continue
        fe_path_norm = normalize_path(fe["path"])
        fe_method = fe.get("method")

        # Check for any active contract match
        found_active = False
        for c in contracts:
            if is_excluded(c["method"], c["path"]):
                continue
            if c.get("status") not in _SATISFIED:
                continue
            c_method = c["method"]
            c_path_norm = normalize_path(c["path"])
            # Match logic: if FE has method, exact match; if method-less, match by path
            if fe_method:
                if fe_method.upper() == c_method and fe_path_norm == c_path_norm:
                    found_active = True
                    break
            else:
                if fe_path_norm == c_path_norm:
                    found_active = True
                    break

        # Check for any dead contract match (already reported in 5a)
        found_dead = False
        for c in contracts:
            if is_excluded(c["method"], c["path"]):
                continue
            if c.get("status") not in _DEAD:
                continue
            c_method = c["method"]
            c_path_norm = normalize_path(c["path"])
            # Match logic: if FE has method, exact match; if method-less, match by path
            if fe_method:
                if fe_method.upper() == c_method and fe_path_norm == c_path_norm:
                    found_dead = True
                    break
            else:
                if fe_path_norm == c_path_norm:
                    found_dead = True
                    break

        if not found_active and not found_dead:
            findings.append({
                "category": "fe_unbacked_path",
                "severity": "advisory",
                "method": fe_method or "",
                "path": fe["path"],
                "contract_id": None,
                "detail": "frontend references path with no contract (active or dead)",
            })

    # -------- Category 6: duplicate_active -------- #
    # Two or more active contracts share the same _key.
    for key, contracts_at_key in active_by_key.items():
        if len(contracts_at_key) > 1:
            method, path = key
            ids_str = ", ".join(str(c.get("id")) for c in sorted(contracts_at_key, key=lambda c: c.get("id", 0)))
            # Emit ONE finding per colliding key
            findings.append({
                "category": "duplicate_active",
                "severity": "blocking",
                "method": method,
                "path": path,
                "contract_id": None,
                "detail": f"duplicate active contracts at {method} {path}: ids {ids_str}",
            })

    # Sort: blocking first, then advisory; within each, by (category, method, path)
    findings.sort(
        key=lambda f: (
            f["severity"] != "blocking",  # blocking=False sorts first
            f["category"],
            f["method"],
            f["path"],
        )
    )

    return findings


# ============================================================================ #
# Thin I/O loaders                                                            #
# ============================================================================ #


def parse_endpoint_registry(text: str) -> list[dict]:
    """Parse apps/web/src/shared/api/endpoints.ts.

    Extract every path string literal ('/...', "/...", or `/...${x}...`).
    Rewrite ${...} -> {} so normalize_path collapses it.
    Returns [{method: None, path, source:'endpoints.ts'}].
    """
    findings = []
    # Match string literals starting with /
    pattern = r"[`'\"](/[A-Za-z0-9_\-/:${}.]*)[`'\"]"
    for match in re.finditer(pattern, text):
        path = match.group(1)
        # Rewrite ${...} -> {id} to match normalize_path expectations
        path = re.sub(r"\$\{[^}]*\}", "{id}", path)
        if path.startswith("/"):
            findings.append({
                "method": None,
                "path": path,
                "source": "endpoints.ts",
            })
    # Dedupe by path
    seen = set()
    unique = []
    for f in findings:
        key = f["path"]
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def parse_msw_handlers(text: str) -> list[dict]:
    """Parse apps/web/src/test/mocks/handlers.ts.

    Match http.<verb>(`${...}/path`...) and extract method and path.
    Returns [{method: METHOD, path, source:'handlers.ts'}].
    """
    findings = []
    # Pattern: http.(get|post|put|patch|delete)(`${API_VAR}/path`)
    pattern = r"http\.(get|post|put|patch|delete)\(\s*`\$\{[A-Za-z_][A-Za-z0-9_]*\}(/[^`]*)`"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        method = match.group(1).upper()
        path = match.group(2)
        findings.append({
            "method": method,
            "path": path,
            "source": "handlers.ts",
        })
    # Dedupe by (method, path)
    seen = set()
    unique = []
    for f in findings:
        key = (f["method"], f["path"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def parse_frontend_endpoints(repo_root: Path) -> list[dict]:
    """Read both FE files under repo_root and concat parse results.

    Files:
      apps/web/src/shared/api/endpoints.ts  (path-only refs)
      apps/web/src/test/mocks/handlers.ts   (method+path refs)
    Missing file => skip that source (no crash).
    Returns combined list.
    """
    endpoints = []

    # Try endpoints.ts
    endpoints_file = repo_root / "apps/web/src/shared/api/endpoints.ts"
    if endpoints_file.is_file():
        try:
            text = endpoints_file.read_text(encoding="utf-8")
            endpoints.extend(parse_endpoint_registry(text))
        except (OSError, UnicodeDecodeError):
            pass

    # Try handlers.ts
    handlers_file = repo_root / "apps/web/src/test/mocks/handlers.ts"
    if handlers_file.is_file():
        try:
            text = handlers_file.read_text(encoding="utf-8")
            endpoints.extend(parse_msw_handlers(text))
        except (OSError, UnicodeDecodeError):
            pass

    return endpoints


def load_drift_inputs(pool: ConnectionPool, settings: Settings) -> dict:
    """Assemble the four pure-core arguments from live sources.

    Returns {'contracts','audit_routes','fe_endpoints','excluded'} — exactly the
    kwargs of diff_contracts.
    """
    # 1. Load all contracts (all statuses)
    contracts = repository.list_contracts(pool)

    # 2. Resolve and load the audit
    src = (settings.promote_repo_path or settings.apply_repo_path or "").strip()
    if not src:
        raise ValueError("promote_repo_path or apply_repo_path not configured")

    audit_root = repository.resolve_contract_audit_repo(Path(src).expanduser())
    audit_data = repository.load_contract_audit(audit_root)
    audit_routes = audit_data.get("rows", [])

    # 3. Parse frontend endpoints
    fe_endpoints = parse_frontend_endpoints(Path(src).expanduser())

    # 4. Get exclusions
    excluded = repository._BULK_IMPORT_EXCLUDED

    return {
        "contracts": contracts,
        "audit_routes": audit_routes,
        "fe_endpoints": fe_endpoints,
        "excluded": excluded,
    }


def run_drift_check(pool: ConnectionPool, settings: Settings) -> dict:
    """Convenience wrapper for CLI + dashboard.

    Returns {
      'findings': [...],
      'summary': {
        'blocking': n,
        'advisory': m,
        'total': t,
        'by_category': {cat: count}
      },
      'audit_path': str,
      'fe_root': str
    }.
    """
    inputs = load_drift_inputs(pool, settings)
    findings = diff_contracts(**inputs)

    # Compute summary
    blocking = sum(1 for f in findings if f["severity"] == "blocking")
    advisory = sum(1 for f in findings if f["severity"] == "advisory")
    total = len(findings)

    by_category = {}
    for f in findings:
        cat = f["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    # Audit path from audit_root
    src = (settings.promote_repo_path or settings.apply_repo_path or "").strip()
    audit_root = repository.resolve_contract_audit_repo(Path(src).expanduser())
    audit_path = str(audit_root / "packages/contracts/contracts.audit.json")
    fe_root = src

    return {
        "findings": findings,
        "summary": {
            "blocking": blocking,
            "advisory": advisory,
            "total": total,
            "by_category": by_category,
        },
        "audit_path": audit_path,
        "fe_root": fe_root,
    }


def drift_for_contracts(
    pool: ConnectionPool,
    settings: Settings,
    contract_ids,
) -> dict:
    """Fail-safe, scoped drift lookup for the apply-time canonical-route gate.

    Runs the full drift check and returns only the findings whose ``contract_id``
    is in ``contract_ids``, split into blocking/advisory. NEVER raises: if the
    product repo / audit is unavailable (e.g. a hermetic test env with no
    checkout), returns empty findings with ``ok=False`` so callers no-op and
    behaviour is unchanged. This keeps the lifecycle apply hermetic and robust —
    a missing or unreadable product tree can never block a legitimate change.
    """
    ids = set(contract_ids)
    try:
        result = run_drift_check(pool, settings)
    except Exception:  # noqa: BLE001 - route check must never break apply
        return {"blocking": [], "advisory": [], "ok": False}
    blocking, advisory = [], []
    for f in result.get("findings", []):
        if f.get("contract_id") in ids:
            (blocking if f.get("severity") == "blocking" else advisory).append(f)
    return {"blocking": blocking, "advisory": advisory, "ok": True}
