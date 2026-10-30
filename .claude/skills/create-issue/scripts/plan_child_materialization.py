#!/usr/bin/env python3
"""plan_child_materialization.py — Read-only CHILD_MATERIALIZATION_PLAN_V2 generator.

Reads a delivery-rollup parent issue body (via GitHub CLI or a local fixture file)
and produces a CHILD_MATERIALIZATION_PLAN_V2 YAML plan on stdout.

This script is read-only: it never mutates GitHub Issues.
Mutation is delegated to create_issue_txn.py and the edit-issue skill.

Usage:
    # From a live GitHub issue:
    uv run python3 plan_child_materialization.py --repo owner/repo --issue 254

    # From a local body fixture (for tests / dry-run):
    uv run python3 plan_child_materialization.py --body-file fixture.md --issue 254

    NOTE (dry-run / --body-file limitation):
        When --body-file is used, issue refs found in the body are classified as
        'existing_unverified' instead of 'existing_open' or 'existing_closed'.
        The script does NOT call the GitHub API in dry-run mode.
        Do not rely on status=existing_open/existing_closed for --body-file runs.

Exit codes:
    0  — plan generated successfully (may contain warnings)
    1  — fatal error (bad args, GitHub API failure, unrecoverable parse error)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ChildStatus = Literal[
    "missing",
    "existing_open",
    "existing_closed",
    "existing_unverified",
    "stale_body_only",
    "ambiguous",
]
ChildAction = Literal[
    "create_issue",
    "reuse_and_update_parent",
    "no_op",
    "human_escalation",
    "register_subissue_or_human_escalation",
]

SubissuesReadbackStatus = Literal[
    "ok",
    "api_error",
    "forbidden",
    "not_found",
    "gone",
    "rate_limited",
]


@dataclass
class SubissuesReadback:
    """Result of fetching native GitHub Sub-issues for a parent issue.

    complete == False means the actual state is unknown and the plan
    must not be used for mutation (fail-closed design).
    """
    status: SubissuesReadbackStatus
    items: list[dict] = field(default_factory=list)
    http_status: Optional[int] = None
    stderr: Optional[str] = None
    complete: bool = True

GapReason = Literal[
    "unsupported_child_id_format",
    "checkbox_without_issue_ref_but_title_present",
    "issue_ref_present_but_not_native_subissue",
    "stale_placeholder_with_issue_ref",
    "duplicate_child_id",
    "multiple_issue_refs",
    "missing_title",
    "cross_owner_issue",
    "github_api_422",
    "expected_match_count_not_1",
    "malformed_checkbox",
]

RepairConfidence = Literal["high", "medium", "low"]


@dataclass
class ExistingIssueInfo:
    number: int
    state: str  # "OPEN" | "CLOSED"
    state_reason: Optional[str]  # "COMPLETED" | "NOT_PLANNED" | None
    url: str


@dataclass
class ChildEntry:
    child_id: str
    title: str
    status: ChildStatus
    existing_issue: Optional[ExistingIssueInfo]
    action: ChildAction
    dedupe_key: str
    existing_issue_candidates: list[dict] = field(default_factory=list)


@dataclass
class ParentBodyUpdate:
    section: str
    line_number: int  # 1-based line number in the parent body
    old_line: str
    new_line: str
    expected_match_count: int = 1


@dataclass
class ParserGapEntry:
    """A line in the ## Child Issues section that could not be fully parsed."""
    line_number: int
    raw_line: str
    gap_reason: GapReason
    suggested_repair: Optional[str]
    repair_confidence: RepairConfidence
    minimal_context: dict


@dataclass
class BodyInventory:
    """Body desired state from ## Child Issues section."""
    child_issues_section_found: bool
    start_line: Optional[int]
    end_line: Optional[int]
    candidate_count: int  # total checkbox/bullet candidate lines
    parsed_count: int     # successfully parsed child lines
    parser_gap_report: list[ParserGapEntry] = field(default_factory=list)


@dataclass
class IssueLookup:
    strategy: str
    complete: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class Plan:
    schema_version: int
    repo: str
    generated_at: str
    source_issue_number: int
    body_sha256: str
    parent_issue: int
    parent_mode: str
    closure_mode: str
    issue_lookup: IssueLookup
    body_inventory: Optional[BodyInventory] = None
    github_subissues_actual: Optional[SubissuesReadback] = None
    children: list[ChildEntry] = field(default_factory=list)
    parent_body_updates: list[ParentBodyUpdate] = field(default_factory=list)
    required_issue_creations: list[str] = field(default_factory=list)
    required_subissue_registrations: list[str] = field(default_factory=list)
    required_issue_edits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Two separate regexes for child_id matching:
#
# _CHILD_LINE_RE_CNUM: C254-N format (space or colon separator)
#   e.g. "- C254-1 docs: something" or "- C254-1: docs: something"
#
# _CHILD_LINE_RE_ABCD: A/B/C/D track ID format (colon REQUIRED)
#   e.g. "- [ ] A: Issue body validator"  -- VALID
#   e.g. "- [ ] A Issue body validator"   -- NOT matched; parser_gap=unsupported_child_id_format
#
# NOTE: [A-D] is intentionally limited to A/B/C/D (Issue #328 scope).
# Future track expansion (E/F/…) must be handled in a separate Issue.
_CHILD_LINE_RE_CNUM = re.compile(
    r"^[-*]?\s*"
    r"(?:\[[ xX]?\]\s*)?"                        # optional checkbox
    r"(?:#(?P<leading_ref>\d+)\s*[—–—–-]\s*)?"   # optional leading "#N —"
    r"(?P<child_id>C\d+-\d+)"                     # C254-3 form
    r"[:\s]+"                                      # space or colon separator
    r"(?P<rest>.+)$"
)

_CHILD_LINE_RE_ABCD = re.compile(
    r"^[-*]?\s*"
    r"(?:\[[ xX]?\]\s*)?"                        # optional checkbox
    r"(?:#(?P<leading_ref>\d+)\s*[—–—–-]\s*)?"   # optional leading "#N —"
    r"(?P<child_id>[A-D])"                        # A/B/C/D track ID (not [A-Z])
    r":"                                           # colon is REQUIRED (blocker_1 + blocker_3)
    r"\s*"                                         # optional spaces
    r"(?P<rest>.+)$"
)


def _match_child_line(line: str):
    """Try to match a child line against both C254-N and A/B/C/D patterns.

    Returns the first match object or None.
    """
    m = _CHILD_LINE_RE_CNUM.match(line)
    if m:
        return m
    return _CHILD_LINE_RE_ABCD.match(line)

# Matches an existing GitHub issue reference at the end of a rest string, e.g. "#281"
_ISSUE_REF_RE = re.compile(r"#(?P<number>\d+)")

# Placeholder text patterns (Japanese and common English variants)
_PLACEHOLDER_PATTERNS = [
    r"（未起票）",
    r"\(未起票\)",
    r"\(not yet created\)",
    r"\(TBD\)",
]
_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDER_PATTERNS))

# Matches the parent_mode in the Machine-Readable Contract block
_PARENT_MODE_RE = re.compile(
    r"(?im)^[ \t]*parent_mode\s*:\s*[\"']?(?P<mode>[\w-]+)[\"']?\s*$"
)

# Matches the closure_mode in the Machine-Readable Contract block
_CLOSURE_MODE_RE = re.compile(
    r"(?im)^[ \t]*closure_mode\s*:\s*[\"']?(?P<mode>[\w-]+)[\"']?\s*$"
)

# Matches a candidate checkbox/bullet line (may or may not parse successfully)
_CANDIDATE_LINE_RE = re.compile(
    r"^[-*]?\s*(?:\[[ xX]?\]\s*)?(?:#\d+\s*[—–—–-]\s*)?\S"
)


def _extract_parent_mode(body: str) -> str:
    """Extract parent_mode from the Machine-Readable Contract block.

    Returns 'unknown' when not found (rather than assuming delivery-rollup,
    which would auto-process issues with broken Machine-Readable Contracts).
    """
    m = _PARENT_MODE_RE.search(body)
    if m:
        return m.group("mode")
    return "unknown"


def _extract_closure_mode(body: str) -> str:
    """Extract closure_mode from the Machine-Readable Contract block.

    Returns 'unknown' when not found.
    """
    m = _CLOSURE_MODE_RE.search(body)
    if m:
        return m.group("mode")
    return "unknown"


def _extract_child_issues_section(body: str) -> tuple[list[tuple[int, str]], Optional[int], Optional[int]]:
    """Extract lines from the '## Child Issues' section, including sub-headings.

    Unlike the previous implementation, this version does NOT stop at sub-headings
    (### or deeper). It only stops at a sibling heading (## or higher level).

    Returns:
        - list of (1-based-line-number, line) tuples within the section
        - start_line (1-based) or None if section not found
        - end_line (1-based, exclusive) or None if section not found
    """
    in_section = False
    results: list[tuple[int, str]] = []
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    for i, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "## Child Issues":
            in_section = True
            start_line = i
            continue
        if in_section:
            # Stop only at sibling (##) or parent (#) heading, NOT at sub-headings (###)
            if re.match(r"^#{1,2}\s", stripped):
                end_line = i
                break
            results.append((i, line))
    if in_section and end_line is None:
        end_line = (start_line or 0) + len(results) + 1
    return results, start_line, end_line


def _is_candidate_line(line: str) -> bool:
    """Return True if the line looks like a TOP-LEVEL child candidate (checkbox or bullet).

    Blank lines, prose paragraphs, and sub-headings are not candidates.
    Sub-heading lines (###) are NOT candidates but are included in the section scan.

    IMPORTANT (blocker_2 fix): Only lines with at most 2 leading spaces are considered
    top-level candidates.  Lines with 3+ leading spaces are nested metadata bullets
    (e.g. ``absorbs:``, ``output:``, ``validates:``) and must NOT be counted as candidates.
    This prevents nested bullets from being classified as parser_gap entries.

    Example:
        "- [ ] A: Issue body validator — #327"   → candidate  (0 leading spaces)
        "  - absorbs: #46, #57"                  → NOT candidate (2 leading spaces, but
                                                    the stripped form starts with a bullet
                                                    — allow 0-2 leading spaces for
                                                    top-level, but "  -" is 2-space-indent)
    The rule is: at most 2 leading spaces **before** the bullet/reference character.
    Lines indented 3 or more spaces are considered nested metadata and are ignored.
    """
    # Leading-space count check (top-level = 0 spaces before the bullet/reference character).
    # Lines indented by 2 or more spaces are treated as nested metadata bullets
    # (e.g. "  - absorbs: #46, #57") and are NOT candidates.
    # GitHub Markdown nested list items commonly use 2-space indentation.
    leading_spaces = len(line) - len(line.lstrip(" "))
    if leading_spaces >= 2:
        # Nested metadata bullet (absorbs/output/validates style) — not a candidate
        return False

    stripped = line.strip()
    if not stripped:
        return False
    # Sub-headings are section markers, not candidates
    if re.match(r"^#{1,6}\s", stripped):
        return False
    # Must start with bullet/checkbox or look like a child reference
    return bool(re.match(r"^[-*]", stripped) or re.match(r"^[A-D][:\s]", stripped) or
                re.match(r"^C\d+-\d+", stripped) or re.match(r"^#\d+\s*[—–]", stripped))


def _parse_child_lines(body: str) -> list[dict]:
    """Parse child lines from the '## Child Issues' section only.

    Supports both C254-N format and A:/B:/C:/D: track ID format.

    Returns a list of raw dicts with keys:
        child_id, title_fragment, rest, is_placeholder, raw_issue_refs,
        line_number (1-based), raw_line
    """
    section_lines, _, _ = _extract_child_issues_section(body)
    results = []
    for line_number, line in section_lines:
        m = _match_child_line(line.strip())
        if not m:
            continue
        child_id = m.group("child_id")
        rest = m.group("rest").strip()
        is_placeholder = bool(_PLACEHOLDER_RE.search(rest))
        issue_refs = [int(r) for r in _ISSUE_REF_RE.findall(rest)]
        leading = m.group("leading_ref")
        if leading and int(leading) not in issue_refs:
            issue_refs.insert(0, int(leading))
        results.append(
            {
                "child_id": child_id,
                "rest": rest,
                "is_placeholder": is_placeholder,
                "raw_issue_refs": issue_refs,
                "line_number": line_number,
                "raw_line": line,
            }
        )
    return results


def _build_body_inventory(
    body: str,
    parent_issue: int,
    parent_mode: str,
) -> BodyInventory:
    """Build a BodyInventory from the ## Child Issues section.

    Inventory-first approach: count all candidate lines before parsing,
    then record gaps between candidate_count and parsed_count.
    """
    section_lines, start_line, end_line = _extract_child_issues_section(body)

    if start_line is None:
        return BodyInventory(
            child_issues_section_found=False,
            start_line=None,
            end_line=None,
            candidate_count=0,
            parsed_count=0,
            parser_gap_report=[],
        )

    # Count candidates (lines that look like child items)
    candidate_lines = [(ln, line) for ln, line in section_lines if _is_candidate_line(line)]
    candidate_count = len(candidate_lines)

    # Parse successfully
    parsed = _parse_child_lines(body)
    parsed_count = len(parsed)
    parsed_line_numbers = {p["line_number"] for p in parsed}

    # Build parser_gap_report for candidate lines that didn't parse
    parser_gap_report: list[ParserGapEntry] = []
    parsed_child_ids: set[str] = set()
    duplicate_child_ids: set[str] = set()
    for p in parsed:
        cid = p["child_id"]
        if cid in parsed_child_ids:
            duplicate_child_ids.add(cid)
        parsed_child_ids.add(cid)

    for line_number, line in candidate_lines:
        if line_number in parsed_line_numbers:
            # Check for duplicate child_id (unsafe gap)
            stripped = line.strip()
            m = _match_child_line(stripped)
            if m:
                cid = m.group("child_id")
                if cid in duplicate_child_ids:
                    issue_refs = [int(r) for r in _ISSUE_REF_RE.findall(m.group("rest") or "")]
                    if len(issue_refs) > 1:
                        gap_reason: GapReason = "multiple_issue_refs"
                        confidence: RepairConfidence = "low"
                        suggested = None
                    else:
                        gap_reason = "duplicate_child_id"
                        confidence = "low"
                        suggested = None
                    parser_gap_report.append(ParserGapEntry(
                        line_number=line_number,
                        raw_line=line,
                        gap_reason=gap_reason,
                        suggested_repair=suggested,
                        repair_confidence=confidence,
                        minimal_context={
                            "parent_issue": parent_issue,
                            "section": "Child Issues",
                            "raw_line": line.strip(),
                        },
                    ))
            continue

        # This candidate line didn't parse — classify the gap
        stripped = line.strip()
        gap = _classify_parser_gap(
            line_number=line_number,
            raw_line=line,
            stripped=stripped,
            parent_issue=parent_issue,
        )
        if gap is not None:
            parser_gap_report.append(gap)

    return BodyInventory(
        child_issues_section_found=True,
        start_line=start_line,
        end_line=end_line,
        candidate_count=candidate_count,
        parsed_count=parsed_count,
        parser_gap_report=parser_gap_report,
    )


def _classify_parser_gap(
    *,
    line_number: int,
    raw_line: str,
    stripped: str,
    parent_issue: int,
) -> Optional[ParserGapEntry]:
    """Classify a candidate line that failed to parse into a ParserGapEntry.

    Returns None if the line is not actually a candidate (shouldn't happen).

    Gap classification rules:
    - repairable (high confidence): unsupported format, missing issue ref but title present
    - unsafe (low confidence / human_escalation): duplicate_child_id, multiple refs, missing title
    """
    minimal_context = {
        "parent_issue": parent_issue,
        "section": "Child Issues",
        "raw_line": stripped,
    }

    # Check for multiple issue refs — unsafe
    issue_refs = [int(r) for r in _ISSUE_REF_RE.findall(stripped)]
    if len(issue_refs) > 1:
        return ParserGapEntry(
            line_number=line_number,
            raw_line=raw_line,
            gap_reason="multiple_issue_refs",
            suggested_repair=None,
            repair_confidence="low",
            minimal_context=minimal_context,
        )

    # Check if it looks like a single-letter child ID without proper separator
    # e.g. "- A Issue body validator" (space separator, no colon) — repairable
    m_alpha = re.match(
        r"^[-*]?\s*(?:\[[ xX]?\]\s*)?(?:#\d+\s*[—–—–-]\s*)?([A-Z])\s+(\S.*)$", stripped
    )
    if m_alpha:
        child_id_candidate = m_alpha.group(1)
        rest_candidate = m_alpha.group(2).strip()
        if rest_candidate:
            suggested = f"- [ ] {child_id_candidate}: {rest_candidate}"
            return ParserGapEntry(
                line_number=line_number,
                raw_line=raw_line,
                gap_reason="unsupported_child_id_format",
                suggested_repair=suggested,
                repair_confidence="high",
                minimal_context=minimal_context,
            )

    # Check for missing title (only whitespace after child_id or ref)
    m_no_title = re.match(
        r"^[-*]?\s*(?:\[[ xX]?\]\s*)?(?:#\d+\s*[—–—–-]\s*)?(?:C\d+-\d+|[A-Z])[:\s]*$",
        stripped,
    )
    if m_no_title:
        return ParserGapEntry(
            line_number=line_number,
            raw_line=raw_line,
            gap_reason="missing_title",
            suggested_repair=None,
            repair_confidence="low",
            minimal_context=minimal_context,
        )

    # Fallback: malformed checkbox / unknown format
    return ParserGapEntry(
        line_number=line_number,
        raw_line=raw_line,
        gap_reason="malformed_checkbox",
        suggested_repair=None,
        repair_confidence="low",
        minimal_context=minimal_context,
    )


# ---------------------------------------------------------------------------
# GitHub API helpers (read-only)
# ---------------------------------------------------------------------------

def _fetch_issue_body(repo: str, issue_number: int, gh_bin: str = "gh") -> str:
    """Fetch the issue body text from GitHub. Raises RuntimeError on failure."""
    args = [
        gh_bin,
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "body",
        "--jq",
        ".body",
    ]
    cp = subprocess.run(args, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(
            f"gh issue view failed (exit {cp.returncode}): {cp.stderr.strip()}"
        )
    return cp.stdout.strip()


def _view_issue(repo: str, issue_number: int, gh_bin: str = "gh") -> Optional[ExistingIssueInfo]:
    """Fetch a single issue's number, title, state, stateReason, url via gh issue view.

    Returns None and records warning if the call fails (issue is truly ambiguous in that case).
    """
    args = [
        gh_bin,
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "number,title,state,stateReason,url",
    ]
    cp = subprocess.run(args, capture_output=True, text=True)
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout.strip())
        return ExistingIssueInfo(
            number=data["number"],
            state=data.get("state", "UNKNOWN"),
            state_reason=data.get("stateReason"),
            url=data.get("url", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def _fetch_subissues_actual(
    repo: str, parent_issue: int, gh_bin: str = "gh"
) -> SubissuesReadback:
    """Fetch native GitHub Sub-issues for a parent issue via REST API.

    Returns a SubissuesReadback with status and items.
    Fail-closed design: API errors are never silently treated as empty lists.

    status mapping:
        "ok"           — successful fetch (items may be empty list = 0 sub-issues)
        "forbidden"    — HTTP 403
        "not_found"    — HTTP 404
        "gone"         — HTTP 410
        "api_error"    — HTTP 422 or other non-2xx
        "rate_limited" — HTTP 429 or rate-limit signal in stderr

    complete == False means actual state is unknown; plan MUST NOT be used for mutation.
    """
    args = [
        gh_bin,
        "api",
        f"repos/{repo}/issues/{parent_issue}/sub_issues",
        "--jq",
        "[.[] | {number: .number, title: .title, state: .state, url: .html_url}]",
    ]
    cp = subprocess.run(args, capture_output=True, text=True)

    if cp.returncode != 0:
        stderr = cp.stderr.strip()
        # Try to detect HTTP status from gh CLI stderr output
        # gh CLI emits e.g. "HTTP 403: ..." or "GraphQL: ..."
        http_status: Optional[int] = None
        m_http = re.search(r"\bHTTP\s+(\d{3})\b", stderr)
        if m_http:
            http_status = int(m_http.group(1))

        if http_status == 403:
            return SubissuesReadback(
                status="forbidden", items=[], http_status=403, stderr=stderr, complete=False
            )
        if http_status == 404:
            return SubissuesReadback(
                status="not_found", items=[], http_status=404, stderr=stderr, complete=False
            )
        if http_status == 410:
            return SubissuesReadback(
                status="gone", items=[], http_status=410, stderr=stderr, complete=False
            )
        if http_status == 429 or "rate limit" in stderr.lower() or "rate-limit" in stderr.lower():
            return SubissuesReadback(
                status="rate_limited", items=[], http_status=http_status, stderr=stderr, complete=False
            )
        # 422 or any other error
        return SubissuesReadback(
            status="api_error", items=[], http_status=http_status, stderr=stderr, complete=False
        )

    try:
        result = json.loads(cp.stdout.strip() or "[]")
        if isinstance(result, list):
            return SubissuesReadback(status="ok", items=result, complete=True)
        return SubissuesReadback(
            status="api_error", items=[], stderr="Unexpected non-list JSON response", complete=False
        )
    except json.JSONDecodeError as exc:
        return SubissuesReadback(
            status="api_error", items=[], stderr=f"JSON decode error: {exc}", complete=False
        )


def _search_dedupe_candidates(
    repo: str, dedupe_key: str, gh_bin: str = "gh"
) -> list[dict]:
    """Search for existing issues matching a dedupe_key in all states.

    Returns a list of candidate dicts (may be empty).
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--search",
        dedupe_key,
        "--json",
        "number,title,state,url",
        "--limit",
        "10",
    ]
    cp = subprocess.run(args, capture_output=True, text=True)
    if cp.returncode != 0:
        return []
    try:
        return json.loads(cp.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------

def _classify_child(
    parsed: dict,
    *,
    parent_issue: int,
    parent_mode: str,
    repo: str,
    gh_bin: str = "gh",
    dry_run: bool = False,
    issue_lookup_warnings: list[str],
    subissues_actual: list[dict],
    subissues_readback_complete: bool = True,
) -> ChildEntry:
    """Classify a single parsed child line into a ChildEntry.

    Parameters
    ----------
    parsed:
        Raw dict from _parse_child_lines.
    parent_issue:
        Parent issue number (used for dedupe_key).
    parent_mode:
        Parent mode string ('delivery-rollup', 'unknown', etc.).
    repo:
        owner/repo string. Empty string in dry-run mode.
    gh_bin:
        Path to gh CLI binary.
    dry_run:
        When True, no GitHub API calls are made.
    issue_lookup_warnings:
        Mutable list to append lookup warnings to.
    subissues_actual:
        Native GitHub Sub-issues read-back items (actual state). Empty list when
        readback was incomplete.
    subissues_readback_complete:
        False when _fetch_subissues_actual returned an error. When False, any
        child that would require Sub-issue registration is routed to
        human_escalation instead of register_subissue_or_human_escalation.
    """
    child_id: str = parsed["child_id"]
    rest: str = parsed["rest"]
    is_placeholder: bool = parsed["is_placeholder"]
    raw_issue_refs: list[int] = parsed["raw_issue_refs"]

    # parent_mode unknown → human_escalation for this child
    if parent_mode == "unknown":
        dedupe_key = f"unknown:{parent_issue}:{child_id}"
        title = _build_title(rest)
        return ChildEntry(
            child_id=child_id,
            title=title,
            status="ambiguous",
            existing_issue=None,
            action="human_escalation",
            dedupe_key=dedupe_key,
            existing_issue_candidates=[],
        )

    dedupe_key = f"{parent_mode}:{parent_issue}:{child_id}"

    # Strip trailing issue references and placeholder text to get a clean title
    title = _build_title(rest)

    if is_placeholder and not raw_issue_refs:
        # Body says "(未起票)" with no issue ref → missing
        return ChildEntry(
            child_id=child_id,
            title=title,
            status="missing",
            existing_issue=None,
            action="create_issue",
            dedupe_key=dedupe_key,
            existing_issue_candidates=[],
        )

    if raw_issue_refs:
        issue_number = raw_issue_refs[0]

        if dry_run:
            # In dry-run mode, we cannot verify issue state
            status: ChildStatus = "existing_unverified"
            if is_placeholder:
                status = "stale_body_only"

            # Check if issue_number appears in subissues_actual (even in dry_run if provided)
            actual_match = any(s.get("number") == issue_number for s in subissues_actual)
            if not actual_match and not is_placeholder and subissues_actual:
                # Body has #N but native sub-issue not registered
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="existing_unverified",
                    existing_issue=ExistingIssueInfo(
                        number=issue_number,
                        state="UNKNOWN",
                        state_reason=None,
                        url="",
                    ),
                    action="register_subissue_or_human_escalation",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=[],
                )

            return ChildEntry(
                child_id=child_id,
                title=title,
                status=status,
                existing_issue=ExistingIssueInfo(
                    number=issue_number,
                    state="UNKNOWN",
                    state_reason=None,
                    url="",
                ),
                action="no_op" if not is_placeholder else "reuse_and_update_parent",
                dedupe_key=dedupe_key,
                existing_issue_candidates=[],
            )

        # Verify issue state via individual gh issue view
        info = _view_issue(repo, issue_number, gh_bin)
        if info is None:
            # API failure for this individual issue → ambiguous (not a plan-level failure)
            issue_lookup_warnings.append(
                f"gh issue view #{issue_number} failed — cannot determine state. "
                f"Classified as ambiguous."
            )
            return ChildEntry(
                child_id=child_id,
                title=title,
                status="ambiguous",
                existing_issue=None,
                action="human_escalation",
                dedupe_key=dedupe_key,
                existing_issue_candidates=[],
            )

        is_open = info.state.upper() == "OPEN"

        # Check if issue_number appears in subissues_actual
        actual_match = any(s.get("number") == issue_number for s in subissues_actual)

        if is_placeholder:
            if is_open:
                # Body says "(未起票)" + issue ref + issue is open → stale body
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="stale_body_only",
                    existing_issue=info,
                    action="reuse_and_update_parent",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=[],
                )
            else:
                # placeholder + closed ref → stale + closed, treat as human_escalation
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="stale_body_only",
                    existing_issue=info,
                    action="human_escalation",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=[],
                )
        else:
            # AC4: body has #N but native Sub-issue parent read-back doesn't match
            # → register_subissue_or_human_escalation (not no_op)
            #
            # Fail-closed (blocker_4): if readback was incomplete (API error),
            # route to human_escalation instead of register_subissue_or_human_escalation.
            # We must not produce a mutation plan when actual state is unknown.
            if not actual_match and subissues_readback_complete is not None:
                if not subissues_readback_complete:
                    # Actual state unknown — human must verify before registering
                    issue_lookup_warnings.append(
                        f"Sub-issue readback was incomplete (API error). "
                        f"Child {child_id} (#{issue_number}) cannot be safely registered. "
                        "Routed to human_escalation."
                    )
                    return ChildEntry(
                        child_id=child_id,
                        title=title,
                        status="ambiguous",
                        existing_issue=info,
                        action="human_escalation",
                        dedupe_key=dedupe_key,
                        existing_issue_candidates=[],
                    )

                candidates = _search_dedupe_candidates(repo, dedupe_key, gh_bin)
                if is_open:
                    return ChildEntry(
                        child_id=child_id,
                        title=title,
                        status="existing_open",
                        existing_issue=info,
                        action="register_subissue_or_human_escalation",
                        dedupe_key=dedupe_key,
                        existing_issue_candidates=candidates,
                    )
                else:
                    return ChildEntry(
                        child_id=child_id,
                        title=title,
                        status="existing_closed",
                        existing_issue=info,
                        action="register_subissue_or_human_escalation",
                        dedupe_key=dedupe_key,
                        existing_issue_candidates=candidates,
                    )

            if is_open:
                # Normal case: body references a known open issue, registered as subissue
                candidates = _search_dedupe_candidates(repo, dedupe_key, gh_bin)
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="existing_open",
                    existing_issue=info,
                    action="no_op",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=candidates,
                )
            else:
                # closed child — in child-complete mode this is a NORMAL success state
                candidates = _search_dedupe_candidates(repo, dedupe_key, gh_bin)
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="existing_closed",
                    existing_issue=info,
                    action="no_op",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=candidates,
                )

    # No placeholder, no issue ref → treat as missing (bare child description)
    return ChildEntry(
        child_id=child_id,
        title=title,
        status="missing",
        existing_issue=None,
        action="create_issue",
        dedupe_key=dedupe_key,
        existing_issue_candidates=[],
    )


def _build_title(rest: str) -> str:
    """Build a clean title string from a raw 'rest' field."""
    title = _PLACEHOLDER_RE.sub("", rest)
    # Remove trailing issue refs like "#281"
    title = re.sub(r"\s*#\d+\s*$", "", title).strip()
    # Remove leading "—" or "–" (em/en dash) when child_id was already stripped
    title = re.sub(r"^[—–-]\s*", "", title).strip()
    # Remove trailing colon/dash artefacts
    title = title.rstrip("-:").strip()
    return title


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------

def build_plan(
    body: str,
    parent_issue: int,
    repo: str = "",
    gh_bin: str = "gh",
    dry_run: bool = False,
) -> Plan:
    """Build a CHILD_MATERIALIZATION_PLAN_V2 from a parent issue body.

    Parameters
    ----------
    body:
        Parent issue body text.
    parent_issue:
        Parent issue number.
    repo:
        owner/repo string. Required for live mode; empty string for dry-run.
    gh_bin:
        Path to gh CLI binary.
    dry_run:
        When True, no GitHub API calls are made (--body-file mode).
    """
    parent_mode = _extract_parent_mode(body)
    closure_mode = _extract_closure_mode(body)
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    issue_lookup_warnings: list[str] = []
    issue_lookup = IssueLookup(
        strategy="referenced_issue_view_and_dedupe_search_all_states",
        complete=True,
        warnings=issue_lookup_warnings,
    )

    if dry_run:
        issue_lookup.strategy = "body_file_dry_run_no_api_calls"

    # Fetch native Sub-issue actual state (AC3: separate actual from desired)
    subissues_readback: Optional[SubissuesReadback] = None
    if not dry_run and repo:
        subissues_readback = _fetch_subissues_actual(repo, parent_issue, gh_bin)

    # Build body inventory (desired state — AC2a)
    body_inventory = _build_body_inventory(body, parent_issue, parent_mode)

    plan = Plan(
        schema_version=2,
        repo=repo,
        generated_at=generated_at,
        source_issue_number=parent_issue,
        body_sha256=body_sha256,
        parent_issue=parent_issue,
        parent_mode=parent_mode,
        closure_mode=closure_mode,
        issue_lookup=issue_lookup,
        body_inventory=body_inventory,
        github_subissues_actual=subissues_readback,
    )

    # Fail-closed: if Sub-issue readback is incomplete, do NOT produce a mutation plan.
    # Consumer skills must check github_subissues_actual.complete before acting.
    if subissues_readback is not None and not subissues_readback.complete:
        plan.warnings.append(
            f"github_subissues_actual.complete=false (status={subissues_readback.status}, "
            f"http_status={subissues_readback.http_status}): actual Sub-issue state is unknown. "
            "All children requiring Sub-issue registration are routed to human_escalation. "
            "Do NOT use this plan for mutation."
        )
        issue_lookup.complete = False

    # Items to use for child classification (empty list if readback incomplete)
    subissues_items: list[dict] = (
        subissues_readback.items
        if subissues_readback is not None and subissues_readback.complete
        else []
    )

    parsed_children = _parse_child_lines(body)

    if not parsed_children:
        plan.warnings.append(
            "No child lines (Cxxx-N or A/B/C/D pattern) found in the '## Child Issues' section. "
            "Verify the body contains delivery-rollup child references under that heading."
        )

    for parsed in parsed_children:
        entry = _classify_child(
            parsed,
            parent_issue=parent_issue,
            parent_mode=parent_mode,
            repo=repo,
            gh_bin=gh_bin,
            dry_run=dry_run,
            issue_lookup_warnings=issue_lookup_warnings,
            subissues_actual=subissues_items,
            subissues_readback_complete=(
                subissues_readback.complete if subissues_readback is not None else True
            ),
        )
        plan.children.append(entry)

        # Collect required_issue_creations
        if entry.action == "create_issue":
            plan.required_issue_creations.append(entry.child_id)

        # Collect required_subissue_registrations (AC4)
        if entry.action == "register_subissue_or_human_escalation":
            if entry.existing_issue is not None:
                plan.required_subissue_registrations.append(
                    f"#{entry.existing_issue.number} → #{parent_issue} ({entry.child_id})"
                )

        # Collect required_issue_edits (parent body update needed)
        if entry.action == "reuse_and_update_parent":
            plan.required_issue_edits.append(
                f"#{parent_issue}: replace stale placeholder for {entry.child_id}"
            )

        # Build parent_body_updates for stale entries (open only)
        if (
            entry.status == "stale_body_only"
            and entry.existing_issue is not None
            and entry.existing_issue.state.upper() == "OPEN"
        ):
            raw_line: str = parsed["raw_line"]
            line_number: int = parsed["line_number"]
            issue_num = entry.existing_issue.number
            # Build new_line by removing placeholder and ensuring issue ref
            new_rest = _PLACEHOLDER_RE.sub("", parsed["rest"]).strip()
            if f"#{issue_num}" not in new_rest:
                new_rest = f"{new_rest} #{issue_num}".strip()
            # Preserve the original line prefix (bullet, checkbox, etc.)
            prefix_m = re.match(r"^([-*]?\s*(?:\[[ xX]?\]\s*)?)", raw_line)
            prefix = prefix_m.group(1) if prefix_m else ""
            new_line = f"{prefix}{entry.child_id} {new_rest}"

            # Count occurrences to detect duplicate matches
            expected = body.count(raw_line.rstrip())
            plan.parent_body_updates.append(
                ParentBodyUpdate(
                    section="Child Issues",
                    line_number=line_number,
                    old_line=raw_line.rstrip(),
                    new_line=new_line,
                    expected_match_count=expected,
                )
            )

    return plan


# ---------------------------------------------------------------------------
# YAML serialization (stdlib only — no PyYAML dependency required)
# ---------------------------------------------------------------------------

def _quote_yaml_str(s: str) -> str:
    """Return a safely double-quoted YAML string."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def plan_to_yaml(plan: Plan) -> str:
    """Serialize Plan to YAML string (CHILD_MATERIALIZATION_PLAN_V2 format)."""
    lines: list[str] = ["CHILD_MATERIALIZATION_PLAN_V2:"]
    lines.append(f"  schema_version: {plan.schema_version}")
    lines.append(f"  repo: {_quote_yaml_str(plan.repo)}")
    lines.append(f"  generated_at: {_quote_yaml_str(plan.generated_at)}")
    lines.append("  source:")
    lines.append("    kind: parent_issue_body")
    lines.append(f"    issue_number: {plan.source_issue_number}")
    lines.append(f"    body_sha256: {_quote_yaml_str(plan.body_sha256)}")
    lines.append("  parent:")
    lines.append(f"    issue_number: {plan.parent_issue}")
    lines.append(f"    parent_mode: {plan.parent_mode}")
    lines.append(f"    closure_mode: {plan.closure_mode}")
    lines.append("  issue_lookup:")
    lines.append(f"    strategy: {_quote_yaml_str(plan.issue_lookup.strategy)}")
    lines.append(f"    complete: {'true' if plan.issue_lookup.complete else 'false'}")
    lines.append("    warnings:")
    if not plan.issue_lookup.warnings:
        lines.append("      []")
    else:
        for w in plan.issue_lookup.warnings:
            lines.append(f"      - {_quote_yaml_str(w)}")

    # Body inventory (AC2a, AC3)
    lines.append("  body_inventory:")
    if plan.body_inventory is None:
        lines.append("    null")
    else:
        bi = plan.body_inventory
        lines.append(f"    child_issues_section_found: {'true' if bi.child_issues_section_found else 'false'}")
        lines.append(f"    start_line: {'null' if bi.start_line is None else bi.start_line}")
        lines.append(f"    end_line: {'null' if bi.end_line is None else bi.end_line}")
        lines.append(f"    candidate_count: {bi.candidate_count}")
        lines.append(f"    parsed_count: {bi.parsed_count}")
        lines.append("    parser_gap_report:")
        if not bi.parser_gap_report:
            lines.append("      []")
        else:
            for gap in bi.parser_gap_report:
                lines.append(f"      - line_number: {gap.line_number}")
                lines.append(f"        raw_line: {_quote_yaml_str(gap.raw_line)}")
                lines.append(f"        gap_reason: {gap.gap_reason}")
                sr = gap.suggested_repair
                lines.append(f"        suggested_repair: {'null' if sr is None else _quote_yaml_str(sr)}")
                lines.append(f"        repair_confidence: {gap.repair_confidence}")
                lines.append("        minimal_context:")
                for k, v in gap.minimal_context.items():
                    lines.append(f"          {k}: {_quote_yaml_str(str(v))}")

    # GitHub Sub-issues actual state (AC3)
    lines.append("  github_subissues_actual:")
    if plan.github_subissues_actual is None:
        lines.append("    status: not_fetched")
        lines.append("    complete: true")
        lines.append("    items: []")
    else:
        rb = plan.github_subissues_actual
        lines.append(f"    status: {rb.status}")
        lines.append(f"    complete: {'true' if rb.complete else 'false'}")
        if rb.http_status is not None:
            lines.append(f"    http_status: {rb.http_status}")
        if rb.stderr:
            lines.append(f"    stderr: {_quote_yaml_str(rb.stderr)}")
        lines.append("    items:")
        if not rb.items:
            lines.append("      []")
        else:
            for si in rb.items:
                lines.append(f"      - number: {si.get('number', '')}")
                lines.append(f"        title: {_quote_yaml_str(str(si.get('title', '')))}")
                lines.append(f"        state: {si.get('state', '')}")
                lines.append(f"        url: {_quote_yaml_str(str(si.get('url', '')))}")

    lines.append("  children:")

    if not plan.children:
        lines.append("    []")
    else:
        for child in plan.children:
            lines.append(f"    - child_id: {child.child_id}")
            lines.append(f"      title: {_quote_yaml_str(child.title)}")
            lines.append(f"      status: {child.status}")
            if child.existing_issue is None:
                lines.append("      existing_issue: null")
            else:
                lines.append("      existing_issue:")
                lines.append(f"        number: {child.existing_issue.number}")
                lines.append(f"        state: {child.existing_issue.state}")
                sr = child.existing_issue.state_reason
                lines.append(f"        state_reason: {'null' if sr is None else sr}")
                lines.append(f"        url: {_quote_yaml_str(child.existing_issue.url)}")
            lines.append(f"      action: {child.action}")
            lines.append(f"      dedupe_key: {_quote_yaml_str(child.dedupe_key)}")
            lines.append("      existing_issue_candidates:")
            if not child.existing_issue_candidates:
                lines.append("        []")
            else:
                for c in child.existing_issue_candidates:
                    lines.append(f"        - number: {c.get('number', '')}")
                    lines.append(f"          title: {_quote_yaml_str(str(c.get('title', '')))}")
                    lines.append(f"          state: {c.get('state', '')}")
                    lines.append(f"          url: {_quote_yaml_str(str(c.get('url', '')))}")

    lines.append("  parent_body_updates:")
    if not plan.parent_body_updates:
        lines.append("    []")
    else:
        for upd in plan.parent_body_updates:
            lines.append(f"    - section: {_quote_yaml_str(upd.section)}")
            lines.append(f"      line_number: {upd.line_number}")
            lines.append(f"      old_line: {_quote_yaml_str(upd.old_line)}")
            lines.append(f"      new_line: {_quote_yaml_str(upd.new_line)}")
            lines.append(f"      expected_match_count: {upd.expected_match_count}")

    lines.append("  required_issue_creations:")
    if not plan.required_issue_creations:
        lines.append("    []")
    else:
        for item in plan.required_issue_creations:
            lines.append(f"    - {item}")

    lines.append("  required_subissue_registrations:")
    if not plan.required_subissue_registrations:
        lines.append("    []")
    else:
        for item in plan.required_subissue_registrations:
            lines.append(f"    - {_quote_yaml_str(item)}")

    lines.append("  required_issue_edits:")
    if not plan.required_issue_edits:
        lines.append("    []")
    else:
        for item in plan.required_issue_edits:
            lines.append(f"    - {_quote_yaml_str(item)}")

    lines.append("  warnings:")
    if not plan.warnings:
        lines.append("    []")
    else:
        for w in plan.warnings:
            lines.append(f"    - {_quote_yaml_str(w)}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate CHILD_MATERIALIZATION_PLAN_V2 from a delivery-rollup parent issue."
    )
    parser.add_argument(
        "--issue",
        type=int,
        required=True,
        help="Parent issue number (e.g. 254)",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="owner/repo (required when fetching from GitHub)",
    )
    parser.add_argument(
        "--body-file",
        default="",
        help=(
            "Path to a local body fixture file (skips GitHub API call). "
            "Issue refs will be classified as 'existing_unverified'."
        ),
    )
    parser.add_argument(
        "--gh",
        default="gh",
        help="Path to gh CLI binary (default: gh)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 1. Acquire body text
    dry_run = False
    if args.body_file:
        p = Path(args.body_file)
        if not p.is_file():
            sys.stderr.write(f"[ERROR] body-file not found: {args.body_file}\n")
            return 1
        body = p.read_text(encoding="utf-8")
        dry_run = True
        repo = args.repo  # may be empty in dry-run mode
    else:
        if not args.repo:
            sys.stderr.write("[ERROR] --repo is required when --body-file is not provided\n")
            return 1
        try:
            body = _fetch_issue_body(args.repo, args.issue, args.gh)
        except RuntimeError as exc:
            sys.stderr.write(f"[ERROR] {exc}\n")
            return 1
        repo = args.repo

    # 2. Build plan
    plan = build_plan(
        body,
        parent_issue=args.issue,
        repo=repo,
        gh_bin=args.gh,
        dry_run=dry_run,
    )

    # 3. Check if lookup was complete (non-dry-run only)
    if not dry_run and not plan.issue_lookup.complete:
        sys.stderr.write(
            "[WARN] issue_lookup.complete=false — plan is non-actionable. "
            "Consumer skills must NOT mutate GitHub Issues based on this plan.\n"
        )

    # 4. Output YAML
    sys.stdout.write(plan_to_yaml(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
