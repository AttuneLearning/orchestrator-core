"""Shared development-docs browser for the dashboard "Docs" tab.

Read-only listing + rendering of files under settings.docs_path (and subdirs), so
agents and humans share dev docs in one place. No template engine, minimal deps —
matching the dashboard's posture. All path handling is traversal-safe: a requested
relative path is resolved and asserted to stay within the docs root.
"""

from __future__ import annotations

import difflib
import re
from html import escape
from pathlib import Path
from typing import Any, Optional

# Extensions we render inline as text/markdown; everything else offers raw/download.
_TEXT_EXTS = {".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml",
              ".toml", ".ini", ".csv", ".log", ".ts", ".tsx", ".js", ".mjs",
              ".py", ".sh", ".sql", ".css"}
_HTML_EXTS = {".html", ".htm"}
_MD_EXTS = {".md", ".markdown"}
# Skip noise when walking.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", "dist", "build"}


def docs_root(docs_path: str) -> Optional[Path]:
    """The configured docs root as a resolved Path, or None if unset/missing."""
    if not docs_path:
        return None
    p = Path(docs_path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if p.is_dir() else None


def safe_resolve(docs_path: str, rel: str) -> Optional[Path]:
    """Resolve `rel` under the docs root, returning the absolute Path only if it
    stays inside the root and exists. Defeats `..` traversal and symlink escape."""
    root = docs_root(docs_path)
    if root is None:
        return None
    target = (root / (rel or "")).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None  # escaped the root
    return target if target.exists() else None


def list_docs(docs_path: str) -> list[dict[str, Any]]:
    """Every file under the docs root (recursive), newest-first, each with its
    posix relative path, name, parent dir, size, and mtime. Dirs are implied by
    the relative paths (grouped in the template)."""
    root = docs_root(docs_path)
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "rel": rel.as_posix(),
            "name": p.name,
            "dir": rel.parent.as_posix() if rel.parent.as_posix() != "." else "",
            "ext": p.suffix.lower(),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def classify(path: Path) -> str:
    """How the viewer should present this file: 'html' | 'markdown' | 'text' | 'binary'."""
    ext = path.suffix.lower()
    if ext in _HTML_EXTS:
        return "html"
    if ext in _MD_EXTS:
        return "markdown"
    if ext in _TEXT_EXTS:
        return "text"
    return "binary"


def read_text(path: Path, limit: int = 1_000_000) -> str:
    """Read a text file defensively (truncate huge files, tolerate bad bytes)."""
    data = path.read_bytes()[:limit]
    return data.decode("utf-8", errors="replace")


# -- minimal, safe markdown -> HTML (no external dep) ----------------------- #
# Deliberately small: headings, fenced/inline code, bold/italic, links, lists,
# blockquotes, hr, GFM tables, paragraphs. Everything is escaped first, so it can
# never inject HTML — formatting is applied to the escaped text.

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
# A GFM table separator row, e.g. `|---|:--:|` or `--- | ---` (dashes + optional
# alignment colons per cell). Detected on the line *after* a header row.
_TABLE_SEP = re.compile(r"^\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?$")


def _inline(text: str) -> str:
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _LINK.sub(r"<a href='\2' target='_blank' rel='noopener'>\1</a>", text)
    return text


def _table_cells(line: str) -> list[str]:
    """Split an (already-escaped) table row into trimmed cell strings, tolerating
    optional leading/trailing pipes."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _render_table(header: str, rows: list[str]) -> str:
    head = "".join(f"<th>{_inline(c)}</th>" for c in _table_cells(header))
    body = []
    for r in rows:
        cells = "".join(f"<td>{_inline(c)}</td>" for c in _table_cells(r))
        body.append(f"<tr>{cells}</tr>")
    return (f"<table class='doc-table'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def render_markdown(md: str) -> str:
    lines = escape(md).split("\n")
    html: list[str] = []
    list_kind: Optional[str] = None  # 'ul' | 'ol'
    i, n = 0, len(lines)

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            html.append(f"</{list_kind}>")
            list_kind = None

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # fenced code (consume through the closing fence)
        if stripped.startswith("```"):
            close_list()
            html.append("<pre class='doc-code'><code>")
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                html.append(lines[i] + "\n")
                i += 1
            html.append("</code></pre>")
            i += 1  # skip closing fence (or run off the end)
            continue

        if not stripped:
            close_list()
            i += 1
            continue

        # GFM table: a header row followed by a separator row
        if "|" in stripped and i + 1 < n and _TABLE_SEP.match(lines[i + 1].strip()):
            close_list()
            header = stripped
            i += 2
            rows: list[str] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(lines[i])
                i += 1
            html.append(_render_table(header, rows))
            continue

        # headings
        m = re.match(r"(#{1,6})\s+(.*)", stripped)
        if m:
            close_list()
            level = len(m.group(1))
            html.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        # hr
        if re.match(r"^(---|\*\*\*|___)$", stripped):
            close_list()
            html.append("<hr>")
            i += 1
            continue
        # blockquote
        if stripped.startswith("&gt;"):
            close_list()
            html.append(f"<blockquote>{_inline(stripped[4:].strip())}</blockquote>")
            i += 1
            continue
        # ordered list
        mo = re.match(r"\d+\.\s+(.*)", stripped)
        if mo:
            if list_kind != "ol":
                close_list()
                html.append("<ol>")
                list_kind = "ol"
            html.append(f"<li>{_inline(mo.group(1))}</li>")
            i += 1
            continue
        # unordered list
        mu = re.match(r"[-*+]\s+(.*)", stripped)
        if mu:
            if list_kind != "ul":
                close_list()
                html.append("<ul>")
                list_kind = "ul"
            html.append(f"<li>{_inline(mu.group(1))}</li>")
            i += 1
            continue
        # paragraph
        close_list()
        html.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    close_list()
    return "\n".join(html)


def render_diff(old: str, new: str, context: int = 3) -> str:
    """A compact, escaped line diff (old → new) as colored <pre> lines. Long runs
    of unchanged lines are collapsed to `context` lines each side with a marker, so
    a whole-document AI edit stays reviewable."""
    old_lines = (old or "").splitlines()
    new_lines = (new or "").splitlines()
    out: list[str] = []

    def ctx(line: str) -> str:
        return f"<span class='diff-ctx'>  {escape(line)}</span>"

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            block = old_lines[i1:i2]
            if len(block) > 2 * context + 1:
                out += [ctx(l) for l in block[:context]]
                out.append(f"<span class='diff-skip'>  ⋯ {len(block) - 2 * context}"
                           " unchanged line(s) ⋯</span>")
                out += [ctx(l) for l in block[-context:]]
            else:
                out += [ctx(l) for l in block]
            continue
        for l in old_lines[i1:i2]:
            out.append(f"<span class='diff-del'>- {escape(l)}</span>")
        for l in new_lines[j1:j2]:
            out.append(f"<span class='diff-add'>+ {escape(l)}</span>")

    if not out:
        return "<p class='muted'>No changes.</p>"
    return "<pre class='diff'>" + "\n".join(out) + "</pre>"


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
