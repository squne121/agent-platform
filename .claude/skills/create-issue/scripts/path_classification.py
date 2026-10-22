#!/usr/bin/env python3
"""path_classification.py — repo-specific path policy SSOT (Issue #1135 P1a).

Classifies repository paths as ``documentation`` or ``code_runtime`` (or
``other``) so that the C12 docs-only exemption can be gated on a *validated*
classification rather than a self-declared ``change_kind: docs`` alone.

Design notes (#1135 P1a):
  - Classification is NOT ``.md``-extension-only. Repo-specific rules apply: a
    ``.py`` under ``.claude/**`` is code/runtime, ``package.json`` /
    ``pnpm-lock.yaml`` / ``pyproject.toml`` are code/runtime, while ``*.md`` /
    ``docs/**`` markdown / ``CLAUDE.md`` are documentation.
  - ``docs_only`` requires a non-empty Allowed Paths list whose every entry is
    documentation and none is code/runtime. Anything unclassifiable (``other``)
    is treated conservatively as NOT documentation, so it blocks docs-only.

This module is the single source of truth for both the C12 gate and any future
consumer; do not re-implement path classification elsewhere.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Issue #2783: shared no-path marker predicate (`scripts/agent-ops/`). Pure
# policy library only -- not a grammar library import.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_OPS_DIR = _REPO_ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))
from allowed_paths_policy import is_no_path_marker  # noqa: E402

DOCUMENTATION = "documentation"
CODE_RUNTIME = "code_runtime"
OTHER = "other"

# Extensions that are unambiguously executable / runtime source or build config.
_CODE_RUNTIME_EXTS = frozenset(
    {
        ".ts", ".tsx", ".mts", ".cts",
        ".js", ".jsx", ".mjs", ".cjs",
        ".py", ".pyi",
        ".sh", ".bash", ".zsh",
        ".rs", ".go", ".rb", ".java",
        ".css", ".scss",
        ".lock",
    }
)

# Documentation extensions.
_DOC_EXTS = frozenset({".md", ".mdx", ".markdown", ".rst", ".txt"})

# Exact basenames that are code/runtime manifests regardless of extension.
_CODE_RUNTIME_BASENAMES = frozenset(
    {
        "package.json", "package-lock.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
        "pyproject.toml", "uv.lock", "tsconfig.json", "vite.config.ts",
        "vite.config.js", "eslint.config.js", "eslint.config.mjs",
        "playwright.config.ts", "vitest.config.ts", "requirements.txt",
        "Makefile", "Dockerfile",
    }
)

# Path prefixes that force a code/runtime classification (build/CI/runtime trees).
_CODE_RUNTIME_PREFIXES = ("src/", "scripts/", "tests/", ".github/")

# Glob-ish wildcard chars that mean the path is a directory scope, not a file.
_WILDCARD_RE = re.compile(r"[*?\[\]]")

_ALLOWED_PATHS_HEADING_RE = re.compile(r"^[ ]{0,3}##[ \t]+Allowed Paths[ \t]*#*[ \t]*$")
_ANY_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}[ \t]+\S")
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.+?)[ \t]*$")


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _ext(path: str) -> str:
    base = _basename(path)
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def classify_path(path: str) -> str:
    """Classify a single repository path.

    Precedence: explicit code/runtime basename or extension or build/CI prefix
    wins over documentation; a documentation extension wins next; otherwise the
    path is ``other`` (conservatively NOT documentation).
    """
    p = path.strip().strip("`").strip()
    if not p:
        return OTHER
    # Strip only a literal leading "./" (do NOT use lstrip("./"), which would also
    # eat the leading dot of dotfiles/dot-dirs like ".github/" or ".claude/").
    if p.startswith("./"):
        p = p[2:]
    base = _basename(p)
    ext = _ext(p)

    # 1. code/runtime manifests by basename
    if base in _CODE_RUNTIME_BASENAMES:
        return CODE_RUNTIME

    # 2. code/runtime by extension (covers wildcards like `src/**/*.py`, `*.ts`)
    if ext in _CODE_RUNTIME_EXTS:
        return CODE_RUNTIME

    # 3. build / CI / source tree prefixes (covers `src/**`, `scripts/*.py`,
    #    `.github/workflows/...`); a wildcard-only path like `src/**` has no doc ext.
    for prefix in _CODE_RUNTIME_PREFIXES:
        if p == prefix.rstrip("/") or p.startswith(prefix):
            return CODE_RUNTIME

    # 4. documentation by extension
    if ext in _DOC_EXTS:
        return DOCUMENTATION

    # 5. docs/ tree without a recognised extension (e.g. `docs/product/`)
    if p.startswith("docs/") or p == "docs":
        return DOCUMENTATION

    # 6. a bare directory-scope wildcard with no extension under an unknown tree
    #    is unclassifiable → other (conservative).
    if _WILDCARD_RE.search(p):
        return OTHER

    return OTHER


def extract_allowed_paths(body: str) -> list[str]:
    """Extract bullet entries from the ``## Allowed Paths`` section.

    Backtick wrappers and bullet markers are stripped. Returns [] when the
    section is absent or empty.
    """
    lines = body.splitlines()
    n = len(lines)
    out: list[str] = []
    i = 0
    while i < n:
        if _ALLOWED_PATHS_HEADING_RE.match(lines[i]):
            j = i + 1
            while j < n and not _ANY_HEADING_RE.match(lines[j]):
                m = _BULLET_RE.match(lines[j])
                if m:
                    token = m.group(1).strip().strip("`").strip()
                    # Issue #2783: judge no-path marker BEFORE any further
                    # normalization -- a marker line contributes 0 entries.
                    if token and not is_no_path_marker(token):
                        out.append(token)
                j += 1
            break
        i += 1
    return out


def has_code_or_runtime_scope(allowed_paths: list[str]) -> bool:
    """True if any allowed path classifies as code/runtime."""
    return any(classify_path(p) == CODE_RUNTIME for p in allowed_paths)


def is_docs_only_allowed_paths(allowed_paths: list[str]) -> bool:
    """True iff the list is non-empty and every entry is documentation.

    Any code/runtime or unclassifiable (``other``) entry makes this False
    (conservative): a docs-only exemption must not apply to mixed/code scopes.
    """
    if not allowed_paths:
        return False
    return all(classify_path(p) == DOCUMENTATION for p in allowed_paths)
