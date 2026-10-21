#!/usr/bin/env python3
"""allowed_paths_policy.py — shared neutral no-path marker predicate for
``## Allowed Paths`` Issue-body sections (Issue #2783, following #2779
research).

This module is intentionally a small, dependency-free *pure* policy library:
it holds no state, performs no I/O, and is safe to import from any consumer
parser (``baseline_vc_preflight.py``, ``check_issue_overlap.py``, etc.)
without pulling in their own parsing internals. It MUST NOT become a general
grammar library for the whole ``## Allowed Paths`` section (that remains
each consumer's own responsibility) — this module only answers the single
question "is this one already-wrapper-stripped-or-not entry a no-path
marker?".

Recognized no-path markers (0 paths semantics):

- canonical: ``(none)``
- legacy (exact-match only, back-compat): ``読み取り専用。リポジトリ変更なし（既定）``

Judgment order (fixed, per Issue #2783 contract):

1. Strip structural Markdown wrapper (bullet marker, code-fence delimiters,
   backtick wrapping) from the raw entry.
2. Exact-match the wrapper-stripped raw entry against the canonical or
   legacy marker. If it matches, the entry represents 0 paths.
3. Otherwise, the entry is an ordinary candidate path — normalization of
   trailing annotation parens etc. is each consumer's own existing logic,
   not this module's concern.

No lexical heuristic is applied: only these two exact strings (after
structural wrapper removal) are ever recognized as no-path markers. This
module never guesses that an arbitrary Unicode/punctuation-bearing prose
line is a no-path marker, and it never classifies unrelated prose that
happens to follow a legacy marker line.

Import-safe (no side effects at import time).
"""

from __future__ import annotations

import re

# --- canonical / legacy no-path marker tokens -------------------------------

CANONICAL_NO_PATH_MARKER = "(none)"
LEGACY_NO_PATH_MARKER = "読み取り専用。リポジトリ変更なし（既定）"

_NO_PATH_MARKERS = (CANONICAL_NO_PATH_MARKER, LEGACY_NO_PATH_MARKER)

# --- structural wrapper stripping -------------------------------------------

_BULLET_RE = re.compile(r"^\s*[-+*]\s+")
_FENCE_OPEN_RE = re.compile(r"^```[^\n]*\n?")
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")


def _strip_wrapper(entry: str) -> str:
    """Strip Markdown bullet markers, code-fence delimiters, and a single
    layer of backtick wrapping from ``entry``. Does not touch annotation
    parens or any other consumer-specific normalization — that is
    deliberately out of scope for this shared helper."""
    s = entry.strip()

    # Strip a single leading Markdown bullet marker (-, +, *), if present.
    s = _BULLET_RE.sub("", s)
    s = s.strip()

    # Strip a full ```[lang]\n ... \n``` code-fence wrapper, if the entry is
    # wrapped as one (AC8: code-fence-wrapped `(none)`).
    if s.startswith("```"):
        s = _FENCE_OPEN_RE.sub("", s, count=1)
        s = _FENCE_CLOSE_RE.sub("", s, count=1)
        s = s.strip()
        return s

    # Strip a single layer of backtick wrapping (`entry`), if the entry is
    # wrapped as exactly one backtick-quoted token with no trailing
    # annotation text (trailing annotation is a consumer-normalization
    # concern, not this helper's).
    if len(s) >= 2 and s.startswith("`") and s.endswith("`") and s.count("`") == 2:
        s = s[1:-1].strip()

    return s


def is_no_path_marker(entry: str) -> bool:
    """Return True if ``entry`` (a single raw ``## Allowed Paths`` list-item
    string, before any consumer-specific annotation normalization) is the
    canonical no-path marker ``(none)`` or the legacy exact marker
    ``読み取り専用。リポジトリ変更なし（既定）``, after structural Markdown
    wrapper removal (bullet marker / code fence / backtick wrapping).

    Returns False for every other input, including real paths, real paths
    with trailing annotation, and unrelated prose lines (no lexical
    heuristic is applied — see module docstring).
    """
    if entry is None:
        return False
    if not isinstance(entry, str):
        return False

    stripped = _strip_wrapper(entry)
    return stripped in _NO_PATH_MARKERS
