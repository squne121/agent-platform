#!/usr/bin/env python3
"""
anchor_context.py

Pure, deterministic analyzer for anchor comment bodies (Issue #1891, parent
#1890).  This script does NOT perform any GitHub API call.  Its only input is
an already-fetched snapshot artifact -- e.g. the ``raw_issue_snapshot.json``
that ``run_refinement_preflight.py`` writes to
``.claude/artifacts/issue-refinement-loop/<issue_number>/raw_issue_snapshot.json``.

Responsibilities (kept intentionally narrow, see Issue #1891 Out of Scope):

1. ``segment``    -- split a comment body into candidate multi-turn segments
                     using explicit conversation markers (``# you asked`` /
                     ``# chatgpt response``, case/space normalized).  Spans
                     without a marker are labeled ``speaker: unknown``
                     (never auto-promoted to ``owner``).
2. ``candidates`` -- extract bullet-list / prose directive candidates from
                     each segment, each carrying a ``source_span`` (line
                     range) and the owning segment's ``speaker``.  Candidates
                     always carry ``relation: unclassified`` and no single
                     ``final_candidate`` winner is ever selected -- semantic
                     relationship classification (add/replace/retract/...)
                     and "which candidate is the final decision" are
                     explicitly Out of Scope for this script.

Both subcommands are pure functions of the already-fetched body text; neither
performs network I/O, so this module can be safely used as a subprocess or
imported directly by ``run_refinement_preflight.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_SEGMENTS = "ANCHOR_CONTEXT_SEGMENTS_V1"
SCHEMA_CANDIDATES = "ANCHOR_CONTEXT_CANDIDATES_V1"

SPEAKER_UNKNOWN = "unknown"
SPEAKER_OWNER = "owner"
SPEAKER_QUOTED_ASSISTANT = "quoted_assistant"

RELATION_UNCLASSIFIED = "unclassified"

# ---------------------------------------------------------------------------
# Marker detection (case-insensitive, leading/trailing whitespace tolerant)
# ---------------------------------------------------------------------------

_MARKER_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"^\s*#{0,6}\s*you\s+asked\s*:?\s*$", re.IGNORECASE),
        "you_asked",
        SPEAKER_OWNER,
    ),
    (
        re.compile(r"^\s*#{0,6}\s*(chatgpt|assistant)\s+response\s*:?\s*$", re.IGNORECASE),
        "chatgpt_response",
        SPEAKER_QUOTED_ASSISTANT,
    ),
)

_BULLET_LINE_RE = re.compile(r"^\s*[-*]\s+\S.*$")

# Deterministic, non-exhaustive keyword set for prose-directive candidates.
# This is NOT a semantic classifier -- it only decides whether a paragraph is
# *worth surfacing* as a directive candidate (Out of Scope: what it means).
_DIRECTIVE_KEYWORD_RE = re.compile(
    r"\b(should|must|please|instead|actually|let's|we need|need to|"
    r"requirement|revise|replace|drop|remove|add|keep|abandon|no longer|"
    r"going forward|from now on|do not|don't)\b",
    re.IGNORECASE,
)


def _match_marker(line: str) -> "tuple[str, str] | None":
    for pattern, name, speaker in _MARKER_PATTERNS:
        if pattern.match(line.strip()):
            return name, speaker
    return None


# ---------------------------------------------------------------------------
# segment
# ---------------------------------------------------------------------------


def _compute_segments(body: str) -> list[dict[str, Any]]:
    """Internal: split `body` into contiguous, non-overlapping segments.

    Each segment keeps its raw (lineno, text) entries so `candidates()` can
    compute exact source_span values without re-deriving line numbers from
    joined text.
    """
    lines = body.splitlines()
    segments: list[dict[str, Any]] = []
    current: "dict[str, Any] | None" = None

    def _flush(end_line: int) -> None:
        nonlocal current
        if current is not None:
            current["end_line"] = end_line
            segments.append(current)
            current = None

    for lineno, raw_line in enumerate(lines, start=1):
        matched = _match_marker(raw_line)
        if matched:
            _flush(lineno - 1)
            marker_name, speaker = matched
            current = {
                "speaker": speaker,
                "marker": marker_name,
                "start_line": lineno,
                "entries": [],
            }
        else:
            if current is None:
                current = {
                    "speaker": SPEAKER_UNKNOWN,
                    "marker": None,
                    "start_line": lineno,
                    "entries": [],
                }
            current["entries"].append((lineno, raw_line))

    _flush(len(lines))

    for idx, seg in enumerate(segments):
        seg["index"] = idx
        # A marker-only segment (no content lines before the next marker) has
        # no entries; end_line already reflects the correct (possibly empty)
        # span from _flush().
        if seg["entries"]:
            seg["end_line"] = max(seg["end_line"], seg["entries"][-1][0])

    return segments


def segment_body(body: str) -> dict[str, Any]:
    """AC1/AC2: public JSON-serializable segmentation result."""
    lines = body.splitlines()
    line_count = len(lines)
    raw_segments = _compute_segments(body)

    segments_out = []
    for seg in raw_segments:
        text = "\n".join(line_text for _lineno, line_text in seg["entries"])
        segments_out.append(
            {
                "index": seg["index"],
                "speaker": seg["speaker"],
                "marker": seg["marker"],
                "start_line": seg["start_line"],
                "end_line": seg["end_line"],
                "text": text,
            }
        )

    return {
        "schema_version": SCHEMA_SEGMENTS,
        "line_count": line_count,
        "segments": segments_out,
    }


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


def _iter_paragraphs(entries: list[tuple[int, str]]) -> "list[tuple[int, int, str]]":
    """Group contiguous non-blank lines (within a segment) into paragraphs."""
    paragraphs: list[tuple[int, int, str]] = []
    para_lines: list[str] = []
    para_start: "int | None" = None
    para_end: "int | None" = None

    for lineno, text in entries:
        if text.strip() == "":
            if para_lines:
                paragraphs.append((para_start, para_end, "\n".join(para_lines)))
                para_lines = []
                para_start = None
                para_end = None
            continue
        if para_start is None:
            para_start = lineno
        para_end = lineno
        para_lines.append(text)

    if para_lines:
        paragraphs.append((para_start, para_end, "\n".join(para_lines)))

    return paragraphs


def extract_candidates(body: str) -> dict[str, Any]:
    """AC3: extract bullet / prose-directive candidates with source_span.

    Never selects a single `final_candidate` winner and never assigns a
    `relation` other than "unclassified" -- semantic relationship
    classification is explicitly Out of Scope (Issue #1891).
    """
    raw_segments = _compute_segments(body)
    candidates: list[dict[str, Any]] = []

    for seg in raw_segments:
        entries = seg["entries"]
        speaker = seg["speaker"]
        seg_index = seg["index"]

        # 1. Bullet-list lines -- each bullet line is its own candidate.
        for lineno, text in entries:
            if _BULLET_LINE_RE.match(text):
                content = text.strip()
                content = content[1:].strip() if content[:1] in ("-", "*") else content
                candidates.append(
                    {
                        "segment_index": seg_index,
                        "speaker": speaker,
                        "kind": "bullet",
                        "source_span": {"start_line": lineno, "end_line": lineno},
                        "text": content,
                        "relation": RELATION_UNCLASSIFIED,
                    }
                )

        # 2. Prose-directive paragraphs -- contiguous non-bullet, non-blank
        #    lines containing at least one directive keyword.
        non_bullet_entries = [(ln, t) for ln, t in entries if not _BULLET_LINE_RE.match(t)]
        for start_line, end_line, text in _iter_paragraphs(non_bullet_entries):
            if _DIRECTIVE_KEYWORD_RE.search(text):
                candidates.append(
                    {
                        "segment_index": seg_index,
                        "speaker": speaker,
                        "kind": "prose_directive",
                        "source_span": {"start_line": start_line, "end_line": end_line},
                        "text": text,
                        "relation": RELATION_UNCLASSIFIED,
                    }
                )

    # Stable ordering: by appearance (start_line).
    candidates.sort(key=lambda c: (c["source_span"]["start_line"], c["source_span"]["end_line"]))
    for idx, cand in enumerate(candidates):
        cand["index"] = idx

    return {
        "schema_version": SCHEMA_CANDIDATES,
        "candidates": candidates,
        # Explicitly never populated -- "which candidate is final" is a
        # human / main-thread judgment call (Out of Scope, Issue #1891).
        "final_candidate": None,
    }


# ---------------------------------------------------------------------------
# source_ranges_covered (AC5): interval-union coverage check
# ---------------------------------------------------------------------------


def compute_source_ranges_covered(segments: "list[dict[str, Any]]", line_count: int) -> bool:
    """AC5: whether the union of segment line ranges covers [1, line_count].

    Uses genuine interval merging (not a naive sum of segment lengths) so
    duplicated/overlapping ranges cannot cause an incorrect `True` result.
    """
    if line_count <= 0:
        return not segments

    intervals = []
    for seg in segments:
        start = seg.get("start_line")
        end = seg.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            continue
        intervals.append((start, end))

    if not intervals:
        return False

    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    if len(merged) != 1:
        return False

    return merged[0][0] == 1 and merged[0][1] == line_count


# ---------------------------------------------------------------------------
# CLI / snapshot-artifact loading
# ---------------------------------------------------------------------------


def extract_anchor_body_from_snapshot(snapshot: dict[str, Any]) -> "str | None":
    """Read the anchor comment body from a `raw_issue_snapshot.json`-shaped
    artifact produced by `run_refinement_preflight.py`.

    This is the ONLY supported input shape (AC8): `anchor_context.py` has no
    GitHub API client of its own and never fetches a comment body itself.
    """
    if not isinstance(snapshot, dict):
        return None
    anchor_comment = snapshot.get("anchor_comment")
    if not isinstance(anchor_comment, dict):
        return None
    body = anchor_comment.get("snapshot")
    if not isinstance(body, str):
        return None
    return body


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subcommand", choices=["segment", "candidates"])
    parser.add_argument(
        "--snapshot-file",
        required=True,
        help=(
            "Path to a raw_issue_snapshot.json artifact produced by "
            "run_refinement_preflight.py (must contain anchor_comment.snapshot)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        raw = Path(args.snapshot_file).read_text(encoding="utf-8")
        snapshot = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"snapshot_file_unreadable: {exc}"}), file=sys.stdout)
        return 2

    body = extract_anchor_body_from_snapshot(snapshot)
    if body is None:
        print(json.dumps({"error": "missing_anchor_comment_snapshot"}))
        return 2

    if args.subcommand == "segment":
        result = segment_body(body)
    else:
        result = extract_candidates(body)

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
