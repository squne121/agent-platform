#!/usr/bin/env python3
"""GitHub PR body validator for LOOP_PROTOCOL."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml

SCHEMA_DECISIONS = {"schema_change", "not_schema_change", "uncertain"}
# Canonical required-section inventory (Issue #2808 AC1). `.github/pull_request_template.md`
# and `.claude/skills/pr-review-judge/references/ac-evidence-checks.md` are materialized
# projections of this single deterministic definition (parity is checked by focused pytest;
# neither file runtime-imports this Python module).
REQUIRED_SECTIONS = [
    "Summary",
    "Checks",
    "Schema Change Applicability",
    "Schema Consumer Inventory",
    "Safety Claim Matrix",
    "Notes",
    # Reviewer-required evidence sections (Issue #2808 AC1 / AC2 / AC3):
    # pr-review-judge already requires these three via
    # `.claude/skills/pr-review-judge/references/ac-evidence-checks.md`. Adding them here
    # closes the authoring/validator split-brain where LP052 previously did not enforce them.
    "受け入れ条件の達成状況",
    "検証コマンド結果",
    "Allowed Paths 遵守",
]
SAFETY_SENSITIVE_PATH_PATTERNS = [
    "transport",
    "permission",
    "sandbox",
    "auth",
    "mcp",
    ".claude/skills/",
    ".github/workflows/",
]
# Deterministic minimum safety-applicability floor (Issue #2808 AC4). These are strong,
# low-false-positive text signals (as opposed to a bare `token` substring, which also matches
# unrelated near-miss wording like "parser token" / "design token" — see AC5). Matching any of
# these against PR body + (when available) linked Issue body means the PR is safety-sensitive
# even when none of SAFETY_SENSITIVE_PATH_PATTERNS matched the changed paths (the #2806 failure
# class: `scripts/summarize_agent_transcript.py` does not match any path pattern above).
SAFETY_SENSITIVE_TEXT_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]*"),  # classic GitHub PAT prefixes: ghp_/gho_/ghu_/ghs_/ghr_
    re.compile(r"github_pat_"),  # fine-grained GitHub PAT prefix
    re.compile(r"(?i)personal access token"),
    re.compile(r"(?i)secret-?like token"),
    re.compile(r"(?i)credential redaction"),
    re.compile(r"(?i)\bredaction\b"),
]
SAFETY_COLUMNS = ["Claim", "Implemented?", "Not controlled", "Evidence", "Follow-up"]
INVENTORY_COLUMNS = ["Consumer ファイル", "更新有無", "備考"]
PLACEHOLDER_SNIPPETS = [
    "（例:",
    "（変更前のキー名・フィールド・型）",
    "（変更後のキー名・フィールド・型）",
    "（rg で列挙したファイル）",
    "yes / no / partial",
]
FENCE_PATTERN = re.compile(r"^(```|~~~)")
HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
YAML_FENCE_PATTERN = re.compile(r"^```(?:yaml|yml)?\s*$", re.IGNORECASE)
YAML_BLOCK_MARKER = "SAFETY_CLAIMS_V1"
FOLLOW_UP_PATTERN = re.compile(r"#\d+")


@dataclass(frozen=True)
class ValidationError:
    rule_id: str
    severity: Literal["error"]
    section: str
    line_start: int
    line_end: int
    message: str
    minimal_context: list[str]
    context_truncated: bool
    fix_hint: str = ""
    autofixable: bool = False


@dataclass(frozen=True)
class ValidationResult:
    schema: str
    target: str
    body_sha256: str
    status: Literal["pass", "fail"]
    errors: list[ValidationError]


def _get_context_lines(
    body: str,
    start_line: int,
    end_line: int,
    max_lines: int = 5,
    max_bytes: int = 2048
) -> tuple[list[str], bool]:
    lines = body.split("\n")
    start = max(0, start_line - 1)
    end = min(len(lines), end_line)
    raw_context = lines[start:end]
    truncated = len(raw_context) > max_lines
    context = raw_context[:max_lines]
    result: list[str] = []
    total_bytes = 0
    for line in context:
        encoded = line.encode("utf-8")
        line_cost = len(encoded) + 1
        if total_bytes + line_cost > max_bytes:
            if total_bytes == 0:
                result.append(encoded[:max_bytes].decode("utf-8", errors="ignore"))
            truncated = True
            break
        result.append(line)
        total_bytes += line_cost
    return result, truncated


def _is_placeholder_text(text: str) -> bool:
    stripped = text.strip()
    if stripped.lower() in {"", "todo", "tbd"}:
        return True
    lowered = stripped.lower()
    if lowered == "n/a":
        return True
    return any(snippet.lower() in lowered for snippet in PLACEHOLDER_SNIPPETS)


def _parse_schema_decision(content: str) -> str | None:
    match = re.search(r"(?im)^\s*-\s*decision:\s*(.+?)\s*$", content)
    if not match:
        return None
    return match.group(1).strip().strip("`")


def _load_changed_paths(changed_paths_file: str | None) -> list[str] | None:
    if not changed_paths_file:
        return None
    paths = Path(changed_paths_file).read_text(encoding="utf-8").splitlines()
    return [path.strip() for path in paths if path.strip()]


def _is_path_safety_sensitive(changed_paths: list[str]) -> bool:
    return any(pattern in path for path in changed_paths for pattern in SAFETY_SENSITIVE_PATH_PATTERNS)


def _is_text_safety_sensitive(texts: list[str]) -> bool:
    combined = "\n".join(text for text in texts if text)
    if not combined:
        return False
    return any(pattern.search(combined) for pattern in SAFETY_SENSITIVE_TEXT_PATTERNS)


def _is_safety_sensitive(changed_paths: list[str] | None, texts: list[str]) -> bool:
    """Deterministic minimum safety-applicability floor (Issue #2808 AC4).

    Input surface is limited to changed_paths + texts (PR body +, when available, linked
    Issue body). `open_pr.py` (create path) and `update_pr.py` (update path) both apply this
    same floor. Reviewer (`pr-review-judge`) may additionally detect safety concerns outside
    this floor; this function is not a replacement for that broader semantic judgment.
    """
    if changed_paths is not None and _is_path_safety_sensitive(changed_paths):
        return True
    return _is_text_safety_sensitive(texts)


def _extract_notes_related_issue(notes_content: str) -> str | None:
    match = re.search(r"(?im)^\s*-\s*Related issue:\s*(.+?)\s*$", notes_content)
    if not match:
        return None
    value = match.group(1).strip()
    if value in {"", "N/A"}:
        return None
    return value


def _find_safety_header_line(content: str) -> tuple[list[str], int] | None:
    for index, line in enumerate(content.splitlines(), 1):
        if line.strip().startswith("|") and "Claim" in line and "Follow-up" in line:
            return [cell.strip() for cell in line.strip().strip("|").split("|")], index
    return None


def _extract_sections(body: str) -> tuple[dict[str, tuple[str, int, int]], dict[str, list[int]]]:
    lines = body.splitlines()
    headings: list[tuple[str, int]] = []
    duplicates: dict[str, list[int]] = {}
    in_fence = False
    fence_token = ""
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if FENCE_PATTERN.match(stripped):
            token = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_token = token
            elif token == fence_token:
                in_fence = False
                fence_token = ""
            continue
        if in_fence:
            continue
        match = HEADING_PATTERN.match(line)
        if not match:
            continue
        name = match.group(1).strip()
        duplicates.setdefault(name, []).append(idx)
        headings.append((name, idx))

    sections: dict[str, tuple[str, int, int]] = {}
    for index, (name, heading_line) in enumerate(headings):
        next_heading = headings[index + 1][1] if index + 1 < len(headings) else len(lines) + 1
        content = "\n".join(lines[heading_line:next_heading - 1]).strip()
        if name not in sections:
            sections[name] = (content, heading_line + 1, next_heading - 1)
    return sections, {name: locs for name, locs in duplicates.items() if len(locs) > 1}


def _extract_safety_claims_yaml(content: str) -> tuple[str | None, int | None, int | None]:
    lines = content.splitlines()
    in_yaml = False
    collected: list[str] = []
    start_line = None
    for idx, line in enumerate(lines, 1):
        if not in_yaml and YAML_FENCE_PATTERN.match(line.strip()):
            in_yaml = True
            start_line = idx + 1
            collected = []
            continue
        if in_yaml and line.strip() == "```":
            block = "\n".join(collected)
            if YAML_BLOCK_MARKER in block or re.search(r"(?m)^safety_claims:\s*$", block):
                return block, start_line, idx - 1
            in_yaml = False
            collected = []
            start_line = None
            continue
        if in_yaml:
            collected.append(line)
    return None, None, None


def _error(
    body: str,
    rule_id: str,
    section: str,
    line_start: int,
    line_end: int,
    message: str,
    fix_hint: str
) -> ValidationError:
    context, truncated = _get_context_lines(body, line_start, line_end)
    return ValidationError(rule_id, "error", section, line_start, line_end, message, context, truncated, fix_hint)


def _validate_lp052(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    errors = []
    for section_name in REQUIRED_SECTIONS:
        if section_name not in sections:
            errors.append(ValidationError(
                "LP052",
                "error",
                "(global)",
                1,
                1,
                f"Missing required section: {section_name}",
                ["(Section not found)"],
                False,
                f"Add '## {section_name}' to the PR body."
            ))
    return errors


def _validate_lp054(body: str, duplicates: dict[str, list[int]]) -> list[ValidationError]:
    return [
        _error(
            body,
            "LP054",
            name,
            locs[1],
            locs[1],
            f"Duplicate section heading is not allowed: {name}",
            f"Keep only one '## {name}' section in the PR body."
        ) for name,
        locs in duplicates.items()
    ]


def _validate_lp053(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    info = sections.get("Schema Change Applicability")
    if not info:
        return []
    content, start_line, end_line = info
    decision = _parse_schema_decision(content)
    if decision in SCHEMA_DECISIONS:
        return []
    return [_error(
        body,
        "LP053",
        "Schema Change Applicability",
        start_line,
        end_line,
        "Schema Change Applicability decision is missing or invalid.",
        "Set decision to schema_change, not_schema_change, or uncertain."
    )]


def _validate_lp050(
    body: str,
    sections: dict[str, tuple[str, int, int]],
    schema_decision_override: str | None = None,
) -> list[ValidationError]:
    schema_info = sections.get("Schema Change Applicability")
    inventory_info = sections.get("Schema Consumer Inventory")
    if not schema_info or not inventory_info:
        return []
    decision = schema_decision_override or _parse_schema_decision(schema_info[0])
    if decision not in SCHEMA_DECISIONS or decision == "not_schema_change":
        return []
    content, start_line, end_line = inventory_info
    missing_parts: list[str] = []
    if _is_placeholder_text(content):
        missing_parts.append("placeholder content")
    if "# before" not in content.lower():
        missing_parts.append("before block")
    if "# after" not in content.lower():
        missing_parts.append("after block")
    if not all(column in content for column in INVENTORY_COLUMNS):
        missing_parts.append("consumer inventory table")
    if not missing_parts:
        return []
    return [_error(
        body,
        "LP050",
        "Schema Consumer Inventory",
        start_line,
        end_line,
        "Schema change PR requires non-placeholder inventory with before/after and consumer table.",
        f"Fill inventory details and remove missing parts: {', '.join(missing_parts)}."
    )]


def _validate_lp051(
    body: str,
    sections: dict[str, tuple[str, int, int]],
    is_safety_sensitive: bool
) -> list[ValidationError]:
    if not is_safety_sensitive:
        return []
    info = sections.get("Safety Claim Matrix")
    if not info:
        return [ValidationError(
            "LP051",
            "error",
            "Safety Claim Matrix",
            1,
            1,
            "Safety-sensitive PR requires Safety Claim Matrix.",
            ["(Section not found)"],
            False,
            "Add Safety Claim Matrix with evidence and follow-up columns."
        )]
    content, start_line, end_line = info
    has_na_reason = re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content)
    if _is_placeholder_text(content) or has_na_reason:
        return [_error(
            body,
            "LP051",
            "Safety Claim Matrix",
            start_line,
            end_line,
            "Safety-sensitive PR cannot leave Safety Claim Matrix empty, placeholder-only, or N/A with reason.",
            "Fill concrete safety claims, evidence, and follow-up."
        )]
    return []


def _validate_lp055(
    body: str,
    sections: dict[str, tuple[str, int, int]],
    is_safety_sensitive: bool
) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, end_line = info
    if not is_safety_sensitive and re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
        return []
    if _extract_safety_claims_yaml(content)[0] is not None:
        return []
    header = _find_safety_header_line(content)
    if header is None:
        return [_error(
            body,
            "LP055",
            "Safety Claim Matrix",
            start_line,
            end_line,
            "Safety Claim Matrix header row is missing.",
            "Add table header with Claim / Implemented? / Not controlled / Evidence / Follow-up."
        )]
    columns, relative_line = header
    missing = [column for column in SAFETY_COLUMNS if column not in columns]
    if not missing:
        return []
    line_no = start_line + relative_line - 1
    return [_error(
        body,
        "LP055",
        "Safety Claim Matrix",
        line_no,
        line_no,
        f"Safety Claim Matrix header is missing columns: {', '.join(missing)}.",
        "Restore all required Safety Claim Matrix columns."
    )]


def _validate_lp056(
    body: str,
    sections: dict[str, tuple[str, int, int]],
    is_safety_sensitive: bool
) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, _ = info
    if not is_safety_sensitive and re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
        return []
    if _extract_safety_claims_yaml(content)[0] is not None:
        return []
    for index, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5 or cells[0] == "Claim" or re.match(r"^[-\s]+$", cells[0]):
            continue
        not_controlled = cells[2]
        follow_up = cells[4]
        if not_controlled and not_controlled.lower() not in {
            "",
            "n/a",
            "-",
            "none"
        } and not FOLLOW_UP_PATTERN.search(follow_up):
            line_no = start_line + index - 1
            return [_error(
                body,
                "LP056",
                "Safety Claim Matrix",
                line_no,
                line_no,
                "Not controlled が非空の行には Follow-up の issue 番号が必要です。",
                "Add #<issue> to Follow-up for the uncontrolled claim."
            )]
    return []


def _validate_safety_claims_v1_yaml_contract(
    body: str,
    sections: dict[str, tuple[str, int, int]]
) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, _ = info
    yaml_block, rel_start, rel_end = _extract_safety_claims_yaml(content)
    if yaml_block is None:
        return []
    line_start = start_line + (rel_start or 1) - 1
    line_end = start_line + (rel_end or rel_start or 1) - 1
    try:
        payload = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        return [_error(
            body,
            "E_SAFETY_CLAIMS_PARSE_ERROR",
            "Safety Claim Matrix",
            line_start,
            line_end,
            f"SAFETY_CLAIMS_V1 YAML parse failed: {exc}",
            "Use yaml.safe_load-compatible YAML and remove unsafe tags or invalid syntax."
        )]
    if not isinstance(payload, dict) or not isinstance(payload.get("safety_claims"), list):
        return [_error(
            body,
            "E_SAFETY_CLAIMS_SCHEMA_INVALID",
            "Safety Claim Matrix",
            line_start,
            line_end,
            "SAFETY_CLAIMS_V1 must be a mapping with a safety_claims list.",
            "Set top-level key safety_claims: and provide a list of claim objects."
        )]
    for offset, claim in enumerate(payload["safety_claims"], 1):
        if not isinstance(claim, dict):
            return [_error(
                body,
                "E_SAFETY_CLAIMS_SCHEMA_INVALID",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}] must be a mapping.",
                "Each safety_claims entry must define claim, implemented, evidence, and optional follow_up."
            )]
        if not isinstance(claim.get("claim"), str) or not claim["claim"].strip():
            return [_error(
                body,
                "E_SAFETY_CLAIMS_SCHEMA_INVALID",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}].claim must be a non-empty string.",
                "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md."
            )]
        if claim.get("implemented") not in {"yes", "partial", "no"}:
            return [_error(
                body,
                "E_SAFETY_CLAIMS_SCHEMA_INVALID",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}].implemented must be yes, partial, or no.",
                "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md."
            )]
        evidence = claim.get("evidence")
        if not isinstance(
            evidence,
            list
        ) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
            return [_error(
                body,
                "E_SAFETY_CLAIMS_SCHEMA_INVALID",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}].evidence must contain at least one non-empty string.",
                "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md."
            )]
        not_controlled = claim.get("not_controlled", []) or []
        if not isinstance(
            not_controlled,
            list
        ) or not all(isinstance(item, str) and item.strip() for item in not_controlled):
            return [_error(
                body,
                "E_SAFETY_CLAIMS_SCHEMA_INVALID",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}].not_controlled must be a list of non-empty strings when present.",
                "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md."
            )]
        follow_up = claim.get("follow_up", []) or []
        if not isinstance(follow_up, list) or not all(isinstance(item, str) and item.strip() for item in follow_up):
            return [_error(
                body,
                "E_SAFETY_CLAIMS_SCHEMA_INVALID",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}].follow_up must be a list of non-empty strings when present.",
                "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md."
            )]
        if not_controlled and not follow_up:
            return [_error(
                body,
                "E_FOLLOW_UP_MISSING_CONTRACT",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}] has not_controlled entries but follow_up is empty or missing.",
                "Add at least one #<issue> reference to follow_up for every uncontrolled claim."
            )]
        if not_controlled and not all(FOLLOW_UP_PATTERN.fullmatch(item.strip()) for item in follow_up):
            return [_error(
                body,
                "E_FOLLOW_UP_MISSING_CONTRACT",
                "Safety Claim Matrix",
                line_start,
                line_end,
                f"safety_claims[{offset}] has not_controlled entries but"
                " follow_up does not contain only #<issue> references.",
                "Add #<issue> references to follow_up for every uncontrolled claim."
            )]
    return []


def _validate_lp057_reference_policy(body: str, reference_policy: dict[str, object]) -> list[ValidationError]:
    """LP057 when facts were supplied: follow the single evaluator result (Issue #2878)."""
    if reference_policy.get("decision") != "fail_closed" and reference_policy.get("body_verdict") == "valid":
        return []
    return [_error(
        body,
        "LP057",
        "Notes",
        1,
        1,
        "PR reference policy rejects this body: "
        f"decision={reference_policy.get('decision')} level={reference_policy.get('level')} "
        f"reason_code={reference_policy.get('reason_code')} body_verdict={reference_policy.get('body_verdict')} "
        f"body_reason={reference_policy.get('body_reason')}.",
        "Re-run `validate_pr_body.py --evaluate-reference-policy` and follow its decision "
        "(closing_required -> Closes, nonclosing_required -> Refs, fail_closed -> stop)."
    )]


def _validate_lp057(
    body: str,
    sections: dict[str, tuple[str, int, int]],
    linked_issue: int | None = None,
    reference_policy: dict[str, object] | None = None,
) -> list[ValidationError]:
    if reference_policy is not None:
        return _validate_lp057_reference_policy(body, reference_policy)
    closes_match = re.search(r"(?i)\bCloses\s+#(\d+)\b", body)
    refs_match = re.search(r"(?i)\bRefs\s+#(\d+)\b", body)
    if closes_match or refs_match:
        match = closes_match or refs_match
        matched_issue = int(match.group(1))
        if linked_issue is not None and matched_issue != linked_issue:
            return [_error(
                body,
                "LP057",
                "Notes",
                1,
                1,
                f"PR body references #{matched_issue} but linked issue is #{linked_issue}.",
                f"Update Closes/Refs to reference #{linked_issue}."
            )]
        return []
    notes_info = sections.get("Notes")
    if notes_info:
        notes_related = _extract_notes_related_issue(notes_info[0])
        if notes_related:
            try:
                notes_issue = int(notes_related.lstrip("#"))
                if linked_issue is not None and notes_issue != linked_issue:
                    return [_error(
                        body,
                        "LP057",
                        "Notes",
                        notes_info[1],
                        notes_info[1],
                        f"Related issue references #{notes_issue} but linked issue is #{linked_issue}.",
                        f"Update Related issue to reference #{linked_issue}."
                    )]
                return []
            except (ValueError, AttributeError):
                pass
    line_no = notes_info[1] if notes_info else 1
    return [_error(
        body,
        "LP057",
        "Notes",
        line_no,
        line_no,
        "final PR body must contain Closes/Refs or a filled Related issue reference.",
        "Add Closes #N, Refs #N, or fill Related issue: with a concrete reference."
    )]


def _validate_lp058(body: str, changed_paths: list[str] | None) -> list[ValidationError]:
    if changed_paths is not None and len(changed_paths) > 0:
        return []
    return [ValidationError(
        "LP058",
        "error",
        "(global)",
        1,
        1,
        "changed paths could not be resolved deterministically.",
        ["(changed paths unavailable)"],
        False,
        "Pass --changed-paths-file or resolve changed paths from git diff before validation."
    )]


def _load_implementation_scope_evidence_module():
    """Load the canonical marker parser without creating a shared package (Issue #2811).

    `open_pr.py` loads the same file the same way; this validator reuses the canonical
    `implementation_landed_evidence.py::_parse_marker()` rather than re-implementing it.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "impl-review-loop" / "scripts" / "implementation_landed_evidence.py"
    spec = importlib.util.spec_from_file_location("implementation_landed_evidence_for_validate_pr_body", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_lp059(body: str, linked_issue: int | None) -> list[ValidationError]:
    """Validate-if-present check for the IMPLEMENTATION_SCOPE_COVERAGE_V1 marker (Issue #2811).

    The canonical parser is always the authority; no raw-substring prefilter runs ahead of
    it (a quoted mapping key such as `'IMPLEMENTATION_SCOPE_COVERAGE_V1':` has no raw
    `TOKEN:` substring yet is the marker key after YAML parsing).

    - canonical parser reports `scope_coverage_marker_missing` (genuinely absent): allowed.
    - marker present + canonical parser valid: allowed.
    - anything else (invalid marker, ambiguous marker, unparsable marker fence): fail-closed.
      This single pre-write choke point is shared by `open_pr.py` (create) and
      `update_pr.py` (update), so a malformed marker can never reach `gh pr create` /
      `gh pr edit`.
    """
    try:
        module = _load_implementation_scope_evidence_module()
        if module is None:
            raise ImportError("canonical marker parser could not be loaded")
        marker, parse_errors = module._parse_marker(body, issue_number=linked_issue)
    except Exception as exc:  # fail-closed: an unverifiable marker is never accepted
        return [_error(
            body,
            "LP059",
            "(global)",
            1,
            1,
            f"IMPLEMENTATION_SCOPE_COVERAGE_V1 marker could not be verified: {type(exc).__name__}",
            "Ensure implementation_landed_evidence.py is importable and the marker is well-formed."
        )]
    if marker is not None:
        return []
    if parse_errors == ["scope_coverage_marker_missing"]:
        return []
    return [_error(
        body,
        "LP059",
        "(global)",
        1,
        1,
        f"IMPLEMENTATION_SCOPE_COVERAGE_V1 marker is present but invalid: {', '.join(parse_errors)}",
        "Do not hand-edit the marker; remove it so open-pr's canonical producer can regenerate it."
    )]


# ---------------------------------------------------------------------------
# Reference authority evaluator (Issue #2878)
#
# The single pure evaluator for "which PR -> linked Issue reference is valid". It performs no
# GitHub I/O: the caller (open_pr.py producer, pr-review-judge Step 1, impl-review-loop step-5)
# fetches facts fresh and consumes only the JSON it returns via
# `validate_pr_body.py --evaluate-reference-policy`. The grammar is defined here and nowhere else.
# ---------------------------------------------------------------------------
RESERVED_DEFERRED_DESTINATION_REF = "post-merge-live-evidence"
REFERENCE_DECISION_PREFIX = "Reference-Decision:"
REFERENCE_DECISION_MARKER_TEMPLATE = "REFERENCE_DECISION_V1: nonclosing issue=#{issue}"
A1_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
REFERENCE_FACTS_KEYS = frozenset({"repo", "issue_state", "pr_number", "decision_comment"})
REFERENCE_FACTS_COMMENT_KEYS = frozenset({"url", "id", "issue_url", "author_association", "body"})
REFERENCE_POLICY_OUTPUT_KEYS = (
    "decision",
    "level",
    "reason_code",
    "repo",
    "issue_number",
    "pr_number",
    "pr_body_sha256",
    "effective_kind",
    "body_verdict",
    "body_reason",
)
# The 7 keys that travel producer -> snapshot `non_closing_authority` -> adapter (Issue #2878).
NON_CLOSING_AUTHORITY_KEYS = (
    "decision",
    "level",
    "reason_code",
    "repo",
    "issue_number",
    "pr_number",
    "pr_body_sha256",
)
_REPO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
_RVA_CANONICAL_EN = "Runtime Verification Applicability"
_RVA_DECISIONS = frozenset({"not_applicable", "immediate", "deferred"})
_RVA_DESTINATION_TYPES = frozenset({"issue", "phase", "milestone"})
_RVA_KEY_LINE = re.compile(r"^\s*-?\s*([a-z_]+):[ \t]*(.*?)\s*$")
_RVA_KEYS = frozenset(
    {
        "decision",
        "deferred_destination",
        "destination_type",
        "destination_ref",
        "deferred_verification_condition",
    }
)
# GitHub closing keywords (close/fix/resolve families). Whitespace handling is deliberately
# unambiguous (`[ws]*(?::[ws]*)?`) so a long whitespace run cannot trigger quadratic backtracking.
_CLOSING_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?![A-Za-z0-9_])"
    r"[ \t\r\n]*(?::[ \t\r\n]*)?"
    r"(?:"
    r"https://github\.com/(?P<url_repo>[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+)/issues/(?P<url_num>[0-9]+)(?![0-9])"
    r"|(?P<x_repo>[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+)#(?P<x_num>[0-9]+)(?![0-9])"
    r"|#(?P<num>[0-9]+)(?![0-9])"
    r")",
    re.IGNORECASE,
)
_NON_CLOSING_REFERENCE = re.compile(r"(?<![A-Za-z0-9_])Refs[ \t\r\n]+#(?P<num>[0-9]+)(?![0-9])", re.IGNORECASE)
_A1_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<name>[A-Za-z0-9._-]+)"
    r"/issues/(?P<number>[0-9]+)#issuecomment-(?P<comment_id>[0-9]+)"
)
_LINE_SPLIT = re.compile(r"\r\n|\n|\r")
_prose_boundary_policy_module = None


def _mask_fenced_and_quoted(text: str) -> str:
    """Blank out fenced-code-block lines and quote lines (line structure is preserved)."""
    out: list[str] = []
    in_fence = False
    fence_token = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        newline = "\n" if line.endswith(("\n", "\r")) else ""
        if FENCE_PATTERN.match(stripped):
            token = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_token = token
            elif token == fence_token:
                in_fence = False
                fence_token = ""
            out.append(newline)
            continue
        if in_fence or stripped.startswith(">"):
            out.append(newline)
            continue
        out.append(line)
    return "".join(out)


def _mask_inline_code(text: str) -> str:
    return re.sub(r"(`+)[^\n]*?\1", " ", text)


def _closing_candidates(text: str, default_repo: str) -> list[tuple[str, str]]:
    """Return `(repo_lower, number_string)` for every closing-keyword + issue-target occurrence."""
    candidates: list[tuple[str, str]] = []
    for match in _CLOSING_REFERENCE.finditer(text):
        if match.group("num") is not None:
            candidates.append((default_repo.lower(), match.group("num")))
        elif match.group("x_num") is not None:
            candidates.append((match.group("x_repo").lower(), match.group("x_num")))
        else:
            candidates.append((match.group("url_repo").lower(), match.group("url_num")))
    return candidates


def _assess_reference_text(text: str, repo: str, issue_number: int) -> dict[str, bool]:
    target = (repo.lower(), str(issue_number))
    raw_candidates = _closing_candidates(text, repo)
    outside_fence_quote = _mask_fenced_and_quoted(text)
    outside_candidates = _closing_candidates(outside_fence_quote, repo)
    non_closing_text = _mask_inline_code(outside_fence_quote)
    refs_target = any(m.group("num") == str(issue_number) for m in _NON_CLOSING_REFERENCE.finditer(non_closing_text))
    sections, _ = _extract_sections(text)
    notes_info = sections.get("Notes")
    notes_value = _extract_notes_related_issue(notes_info[0]) if notes_info else None
    notes_only = False
    if notes_value is not None:
        notes_number = notes_value[1:] if notes_value.startswith("#") else notes_value
        notes_only = notes_number == str(issue_number)
    return {
        "closing_other": any(candidate != target for candidate in raw_candidates),
        "closing_target": any(candidate == target for candidate in raw_candidates),
        "closing_target_outside_fence_quote": any(candidate == target for candidate in outside_candidates),
        "refs_target": refs_target,
        "notes_only": notes_only,
    }


def _map_body_verdict(level: str, assessment: dict[str, bool], decision: str) -> tuple[str, str, str]:
    """Return `(effective_kind, body_verdict, body_reason)` for a non-fail_closed decision."""
    if assessment["closing_target"]:
        kind = "closing"
    elif assessment["refs_target"]:
        kind = "non-closing"
    elif assessment["notes_only"]:
        kind = "notes-only"
    else:
        kind = "none"
    if assessment["closing_other"]:
        return kind, "block", "closing_for_other"
    if level == "CLOSED":
        if kind == "closing":
            return kind, "block", "closing_forbidden"
        if kind in {"non-closing", "notes-only"}:
            return kind, "valid", "ok"
        return kind, "block", "reference_missing"
    if decision == "closing_required":
        if kind == "closing":
            if assessment["closing_target_outside_fence_quote"]:
                return kind, "valid", "ok"
            return kind, "repair", "closing_missing"
        if kind == "non-closing":
            return kind, "repair", "closing_missing"
        return kind, "block", "reference_missing"
    # nonclosing_required
    if kind == "non-closing":
        return kind, "valid", "ok"
    if kind == "closing":
        return kind, "block", "closing_forbidden"
    return kind, "block", "reference_missing"


def _load_prose_boundary_policy_module():
    """Path-load the canonical section extractor under a unique module name (Issue #2878)."""
    global _prose_boundary_policy_module
    if _prose_boundary_policy_module is not None:
        return _prose_boundary_policy_module
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "create-issue" / "scripts" / "prose_boundary_policy.py"
    spec = importlib.util.spec_from_file_location("prose_boundary_policy_for_validate_pr_body", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # unique name: dataclass / typing resolution needs it registered
    spec.loader.exec_module(module)
    _prose_boundary_policy_module = module
    return module


def _classify_runtime_applicability(issue_body: str) -> str | None:
    """Return `A2` / `A3`, or `None` when the section is missing / duplicated / unparsable."""
    try:
        module = _load_prose_boundary_policy_module()
        if module is None:
            return None
        first = module.extract_level2_section_with_bounds(issue_body, _RVA_CANONICAL_EN)
        if first is None:
            return None
        section_text, _start, end_index = first
        remaining = "".join(issue_body.splitlines(keepends=True)[end_index:])
        if remaining and module.extract_level2_section_with_bounds(remaining, _RVA_CANONICAL_EN) is not None:
            return None
    except Exception:  # fail-closed: an unverifiable section never yields A2/A3
        return None
    values: dict[str, list[str]] = {}
    for line in section_text.splitlines():
        key_match = _RVA_KEY_LINE.match(line)
        if key_match and key_match.group(1) in _RVA_KEYS:
            values.setdefault(key_match.group(1), []).append(key_match.group(2).strip().strip("`\"'").strip())
    decisions = values.get("decision", [])
    if len(decisions) != 1 or decisions[0] not in _RVA_DECISIONS:
        return None
    if decisions[0] != "deferred":
        return "A3"
    if len(values.get("deferred_destination", [])) != 1:
        return None
    destination_types = values.get("destination_type", [])
    destination_refs = values.get("destination_ref", [])
    conditions = values.get("deferred_verification_condition", [])
    if len(destination_types) != 1 or len(destination_refs) != 1 or len(conditions) != 1:
        return None
    if destination_types[0] not in _RVA_DESTINATION_TYPES or not destination_refs[0] or not conditions[0]:
        return None
    if destination_types[0] == "phase" and destination_refs[0] == RESERVED_DEFERRED_DESTINATION_REF:
        return "A2"
    return "A3"


def _assess_a1(text: str, repo: str, issue_number: int, comment: dict | None) -> str:
    """Return `none` / `valid` / `invalid` / `ambiguous` for the A1 explicit decision."""
    values = [
        line[len(REFERENCE_DECISION_PREFIX):].strip()
        for line in _LINE_SPLIT.split(text)
        if line.startswith(REFERENCE_DECISION_PREFIX)
    ]
    if not values:
        return "none"
    if len(values) >= 2:
        return "ambiguous"
    value = values[0]
    url_match = _A1_URL.fullmatch(value)
    if url_match is None or comment is None:
        return "invalid"
    owner, name = url_match.group("owner"), url_match.group("name")
    if f"{owner}/{name}".lower() != repo.lower() or url_match.group("number") != str(issue_number):
        return "invalid"
    if comment["url"] != value or str(comment["id"]) != url_match.group("comment_id"):
        return "invalid"
    if comment["issue_url"] != f"https://api.github.com/repos/{owner}/{name}/issues/{issue_number}":
        return "invalid"
    if comment["author_association"] not in A1_TRUSTED_ASSOCIATIONS:
        return "invalid"
    marker = REFERENCE_DECISION_MARKER_TEMPLATE.format(issue=issue_number)
    if sum(1 for line in _LINE_SPLIT.split(comment["body"]) if line == marker) != 1:
        return "invalid"
    return "valid"


def _policy_result(
    *,
    decision: str,
    level: str | None,
    reason_code: str,
    repo: str | None,
    issue_number: int | None,
    pr_number: int | None,
    pr_body_sha256: str,
    effective_kind: str = "none",
    body_verdict: str = "block",
    body_reason: str = "not_evaluated",
) -> dict[str, object]:
    return {
        "decision": decision,
        "level": level,
        "reason_code": reason_code,
        "repo": repo,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "pr_body_sha256": pr_body_sha256,
        "effective_kind": effective_kind,
        "body_verdict": body_verdict,
        "body_reason": body_reason,
    }


def _validated_reference_facts(facts: object) -> dict | None:
    """Return the facts when they match the exact wire contract, else `None`."""
    if not isinstance(facts, dict) or set(facts) != REFERENCE_FACTS_KEYS:
        return None
    repo = facts["repo"]
    if not isinstance(repo, str) or _REPO_PATTERN.fullmatch(repo) is None:
        return None
    issue_state = facts["issue_state"]
    # `isinstance(str)` before set membership: an unhashable JSON value (list / object) must be a
    # structured `facts_invalid`, never a `TypeError` from the set lookup.
    if not isinstance(issue_state, str) or issue_state not in {"OPEN", "CLOSED"}:
        return None
    pr_number = facts["pr_number"]
    if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
        return None
    comment = facts["decision_comment"]
    if comment is not None:
        if not isinstance(comment, dict) or set(comment) != REFERENCE_FACTS_COMMENT_KEYS:
            return None
        if type(comment["id"]) is not int or comment["id"] <= 0:
            return None
        if not all(isinstance(comment[key], str) for key in ("url", "issue_url", "author_association", "body")):
            return None
    return facts


def evaluate_reference_policy(
    body_bytes: bytes,
    linked_issue: object,
    linked_issue_body: object,
    facts: object,
) -> dict[str, object]:
    """The single pure PR reference authority evaluator (Issue #2878).

    Inputs are facts only (no GitHub I/O): PR body bytes (hashed as-is, interpreted as UTF-8),
    the linked Issue number, the linked Issue body, and the facts JSON object. Returns the exact
    `decision` / `level` / `reason_code` / ... wire dict. Facts or authority problems yield
    `fail_closed`; ordinary prose that merely resembles a closing keyword never does.
    """
    sha = hashlib.sha256(body_bytes).hexdigest()
    safe_repo = facts.get("repo") if isinstance(facts, dict) and isinstance(facts.get("repo"), str) else None
    safe_issue = linked_issue if type(linked_issue) is int and linked_issue > 0 else None
    safe_pr = None
    if isinstance(facts, dict) and type(facts.get("pr_number")) is int and facts["pr_number"] > 0:
        safe_pr = facts["pr_number"]

    def fail(reason: str, repo: str | None = safe_repo) -> dict[str, object]:
        return _policy_result(
            decision="fail_closed",
            level=None,
            reason_code=reason,
            repo=repo,
            issue_number=safe_issue,
            pr_number=safe_pr,
            pr_body_sha256=sha,
        )

    valid_facts = _validated_reference_facts(facts)
    if valid_facts is None or safe_issue is None or not isinstance(linked_issue_body, str):
        return fail("facts_invalid")
    try:
        text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return fail("facts_invalid")
    repo = valid_facts["repo"]
    assessment = _assess_reference_text(text, repo, safe_issue)

    def finish(decision: str, level: str, reason: str) -> dict[str, object]:
        kind, verdict, body_reason = _map_body_verdict(level, assessment, decision)
        return _policy_result(
            decision=decision,
            level=level,
            reason_code=reason,
            repo=repo,
            issue_number=safe_issue,
            pr_number=valid_facts["pr_number"],
            pr_body_sha256=sha,
            effective_kind=kind,
            body_verdict=verdict,
            body_reason=body_reason,
        )

    if valid_facts["issue_state"] == "CLOSED":
        return finish("nonclosing_required", "CLOSED", "issue_closed")
    a1 = _assess_a1(text, repo, safe_issue, valid_facts["decision_comment"])
    if a1 == "valid":
        return finish("nonclosing_required", "A1", "a1_explicit_decision")
    if a1 == "invalid":
        return fail("a1_decision_invalid", repo)
    if a1 == "ambiguous":
        return fail("a1_decision_ambiguous", repo)
    applicability = _classify_runtime_applicability(linked_issue_body)
    if applicability == "A2":
        return finish("nonclosing_required", "A2", "a2_contract_deferred")
    if applicability == "A3":
        return finish("closing_required", "A3", "a3_close_ready")
    return fail("runtime_applicability_unresolved", repo)


def non_closing_authority_from_result(result: dict[str, object]) -> dict[str, object]:
    """Project an evaluator result onto the 7-key `non_closing_authority` dict."""
    return {key: result.get(key) for key in NON_CLOSING_AUTHORITY_KEYS}


def evaluate_reference_policy_from_files(
    body_bytes: bytes,
    linked_issue: int,
    facts_file: str,
    linked_issue_body_file: str,
) -> dict[str, object]:
    """File-based wrapper used by the CLI: a missing / unreadable / non-JSON input is `facts_invalid`."""
    if not facts_file or not linked_issue_body_file:
        return evaluate_reference_policy(body_bytes, linked_issue, None, None)
    try:
        facts = json.loads(Path(facts_file).read_text(encoding="utf-8"))
        linked_issue_body = Path(linked_issue_body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return evaluate_reference_policy(body_bytes, linked_issue, None, None)
    return evaluate_reference_policy(body_bytes, linked_issue, linked_issue_body, facts)


# ---------------------------------------------------------------------------
# Native auto-close risk check (Issue #2878, PR #2896 review). `nonclosing_required` (A1 / A2) is a
# claim about the PR *body*; GitHub can still close the Issue through a manually linked closing
# relation (Development sidebar) or a closing keyword in the commit message that is actually adopted
# by the chosen merge method. This is a second, pure function over structured facts: it reuses the
# single evaluator result and `_closing_candidates` (no second grammar) and performs no GitHub I/O.
# ---------------------------------------------------------------------------
NATIVE_CLOSE_FACTS_KEYS = frozenset(
    {
        "repo",
        "closing_relations",
        "closing_relations_complete",
        "merge_settings",
        "merge_method",
        "pr_title",
        "pr_body",
        "commit_messages",
        "final_squash_message",
    }
)
NATIVE_CLOSE_SETTINGS_KEYS = frozenset(
    {
        "allow_squash_merge",
        "allow_merge_commit",
        "allow_rebase_merge",
        "squash_merge_commit_title",
        "squash_merge_commit_message",
    }
)
NATIVE_CLOSE_RELATION_KEYS = frozenset({"number", "repository"})
NATIVE_CLOSE_FINAL_MESSAGE_KEYS = frozenset({"title", "body"})
NATIVE_CLOSE_MERGE_METHODS = ("squash", "merge", "rebase")
_SQUASH_TITLE_SETTINGS = frozenset({"PR_TITLE", "COMMIT_OR_PR_TITLE"})
_SQUASH_MESSAGE_SETTINGS = frozenset({"PR_BODY", "COMMIT_MESSAGES", "BLANK"})
NATIVE_AUTO_CLOSE_OUTPUT_KEYS = (
    "status",
    "reason_code",
    "decision",
    "level",
    "repo",
    "issue_number",
    "pr_number",
    "adopted_message_sha256",
    "findings",
)


def _native_close_result(
    policy_result: dict[str, object],
    status: str,
    reason_code: str,
    *,
    adopted_message_sha256: str | None = None,
    findings: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "reason_code": reason_code,
        "decision": policy_result.get("decision"),
        "level": policy_result.get("level"),
        "repo": policy_result.get("repo"),
        "issue_number": policy_result.get("issue_number"),
        "pr_number": policy_result.get("pr_number"),
        "adopted_message_sha256": adopted_message_sha256,
        "findings": findings or [],
    }


def _validated_native_close_facts(facts: object) -> dict | None:
    """Return the native facts when they match the exact wire contract, else `None`."""
    if not isinstance(facts, dict) or set(facts) != NATIVE_CLOSE_FACTS_KEYS:
        return None
    repo = facts["repo"]
    if not isinstance(repo, str) or _REPO_PATTERN.fullmatch(repo) is None:
        return None
    relations = facts["closing_relations"]
    if not isinstance(relations, list) or type(facts["closing_relations_complete"]) is not bool:
        return None
    for relation in relations:
        if not isinstance(relation, dict) or set(relation) != NATIVE_CLOSE_RELATION_KEYS:
            return None
        if type(relation["number"]) is not int or relation["number"] <= 0:
            return None
        relation_repo = relation["repository"]
        if not isinstance(relation_repo, str) or _REPO_PATTERN.fullmatch(relation_repo) is None:
            return None
    settings = facts["merge_settings"]
    if not isinstance(settings, dict) or set(settings) != NATIVE_CLOSE_SETTINGS_KEYS:
        return None
    allow_keys = ("allow_squash_merge", "allow_merge_commit", "allow_rebase_merge")
    if not all(type(settings[key]) is bool for key in allow_keys):
        return None
    # the squash text settings are `null` / absent-equivalent while squash is disabled, so they are
    # only judged (as enum members) when squash is a candidate method.
    for key in ("squash_merge_commit_title", "squash_merge_commit_message"):
        if settings[key] is not None and not isinstance(settings[key], str):
            return None
    method = facts["merge_method"]
    if method is not None and (not isinstance(method, str) or method not in NATIVE_CLOSE_MERGE_METHODS):
        return None
    if not isinstance(facts["pr_title"], str) or not isinstance(facts["pr_body"], str):
        return None
    commits = facts["commit_messages"]
    if not isinstance(commits, list) or not all(isinstance(message, str) for message in commits):
        return None
    final_message = facts["final_squash_message"]
    if final_message is not None:
        if not isinstance(final_message, dict) or set(final_message) != NATIVE_CLOSE_FINAL_MESSAGE_KEYS:
            return None
        if not all(isinstance(final_message[key], str) for key in NATIVE_CLOSE_FINAL_MESSAGE_KEYS):
            return None
    return facts


def _targets_issue(text: str, repo: str, issue_number: int) -> bool:
    """Whether `text` contains a closing keyword aimed at `(repo, issue_number)` (raw text, unmasked)."""
    return any(
        candidate_repo == repo.lower() and int(number) == issue_number
        for candidate_repo, number in _closing_candidates(text, repo)
    )


def _squash_adopted_message(facts: dict) -> tuple[str, str] | None:
    """The `(title, body)` GitHub will adopt for a squash merge, or `None` if the settings are unknown.

    An explicit `final_squash_message` (the exact message that will be submitted) wins; otherwise the
    live `squash_merge_commit_title` / `squash_merge_commit_message` settings decide. The derivation is
    a conservative over-approximation: for `COMMIT_MESSAGES` every commit's full message is included.
    """
    final_message = facts["final_squash_message"]
    if final_message is not None:
        return final_message["title"], final_message["body"]
    settings = facts["merge_settings"]
    title_setting = settings["squash_merge_commit_title"]
    message_setting = settings["squash_merge_commit_message"]
    commits = facts["commit_messages"]
    if title_setting == "PR_TITLE":
        title = facts["pr_title"]
    elif title_setting == "COMMIT_OR_PR_TITLE":
        title = commits[0].split("\n", 1)[0] if len(commits) == 1 else facts["pr_title"]
    else:
        return None
    if message_setting == "PR_BODY":
        body = facts["pr_body"]
    elif message_setting == "COMMIT_MESSAGES":
        body = "\n\n".join(commits)
    elif message_setting == "BLANK":
        body = ""
    else:
        return None
    return title, body


def evaluate_native_auto_close_risk(policy_result: object, native_facts: object) -> dict[str, object]:
    """Pure native auto-close risk check for a `nonclosing_required` (A1 / A2) merge (Issue #2878).

    `policy_result` is the single reference evaluator's output; `native_facts` is a structured
    snapshot the caller fetched fresh (GitHub's closing relation, the live repository merge settings,
    the PR title / body / commit messages, optionally the exact final squash message). The result
    `status` is `clear` (no auto-close path other than the body was found), `blocked` (the target
    Issue would still be auto-closed), `fail_closed` (facts / settings / policy unusable) or
    `not_applicable` (the lane does not promise OPEN). Nothing here closes or merges anything.

    The guarantee is time-bound: a human can change the final squash message or the relation after
    this ran, so the merge-time caller must re-run it on the final message / relation, or merge the
    verified message unchanged (`adopted_message_sha256`). A PR / Issue body hash alone proves neither.
    """
    policy = policy_result if isinstance(policy_result, dict) else {}
    decision = policy.get("decision")
    level = policy.get("level")
    if decision == "fail_closed" or not isinstance(decision, str):
        return _native_close_result(policy, "fail_closed", "reference_policy_fail_closed")
    if decision != "nonclosing_required":
        return _native_close_result(policy, "not_applicable", "closing_required_lane")
    if not isinstance(level, str) or level not in {"A1", "A2"}:
        return _native_close_result(policy, "not_applicable", "issue_closed")
    facts = _validated_native_close_facts(native_facts)
    policy_repo = policy.get("repo")
    issue_number = policy.get("issue_number")
    if (
        facts is None
        or not isinstance(policy_repo, str)
        or facts["repo"].lower() != policy_repo.lower()
        or type(issue_number) is not int
    ):
        return _native_close_result(policy, "fail_closed", "facts_invalid")
    if not facts["closing_relations_complete"]:
        return _native_close_result(policy, "fail_closed", "relations_incomplete")
    settings = facts["merge_settings"]
    allowed = {
        "squash": settings["allow_squash_merge"],
        "merge": settings["allow_merge_commit"],
        "rebase": settings["allow_rebase_merge"],
    }
    requested = facts["merge_method"]
    if requested is not None:
        if not allowed[requested]:
            return _native_close_result(policy, "fail_closed", "merge_method_not_allowed")
        methods = [requested]
    else:
        methods = [method for method in NATIVE_CLOSE_MERGE_METHODS if allowed[method]]
        if not methods:
            return _native_close_result(policy, "fail_closed", "merge_method_not_allowed")
    findings: list[dict[str, str]] = []
    repo = facts["repo"]
    for relation in facts["closing_relations"]:
        if relation["repository"].lower() == repo.lower() and relation["number"] == issue_number:
            findings.append({"kind": "native_relation", "method": "any", "source": "closingIssuesReferences"})
    adopted_sha: str | None = None
    for method in methods:
        if method == "squash":
            if facts["final_squash_message"] is None and (
                settings["squash_merge_commit_title"] not in _SQUASH_TITLE_SETTINGS
                or settings["squash_merge_commit_message"] not in _SQUASH_MESSAGE_SETTINGS
            ):
                return _native_close_result(policy, "fail_closed", "squash_settings_invalid")
            adopted = _squash_adopted_message(facts)
            if adopted is None:
                return _native_close_result(policy, "fail_closed", "squash_settings_invalid")
            title, body = adopted
            adopted_sha = hashlib.sha256(f"{title}\n\n{body}".encode("utf-8")).hexdigest()
            sources = [("squash_title", title), ("squash_body", body)]
        elif method == "merge":
            sources = [("pr_title", facts["pr_title"]), ("pr_body", facts["pr_body"])]
            sources += [("commit_message", message) for message in facts["commit_messages"]]
        else:
            sources = [("commit_message", message) for message in facts["commit_messages"]]
        for source, text in sources:
            if _targets_issue(text, repo, issue_number):
                findings.append({"kind": "effective_message", "method": method, "source": source})
    if findings:
        reason = (
            "native_relation_present"
            if any(finding["kind"] == "native_relation" for finding in findings)
            else "effective_message_closing_keyword"
        )
        return _native_close_result(policy, "blocked", reason, adopted_message_sha256=adopted_sha, findings=findings)
    return _native_close_result(policy, "clear", "no_native_auto_close_path", adopted_message_sha256=adopted_sha)


def evaluate_native_auto_close_risk_from_files(
    body_bytes: bytes,
    linked_issue: int,
    facts_file: str,
    linked_issue_body_file: str,
    native_facts_file: str,
) -> dict[str, object]:
    """File-based wrapper: re-runs the single evaluator, then the native check. Bad input is `facts_invalid`."""
    policy = evaluate_reference_policy_from_files(body_bytes, linked_issue, facts_file, linked_issue_body_file)
    native_facts: object = None
    if native_facts_file:
        try:
            native_facts = json.loads(Path(native_facts_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            native_facts = None
    return evaluate_native_auto_close_risk(policy, native_facts)


def validate_pr_body(
    body: str,
    changed_paths: list[str] | None,
    linked_issue: int | None = None,
    schema_decision_override: str | None = None,
    linked_issue_body: str | None = None,
    reference_policy: dict[str, object] | None = None,
) -> ValidationResult:
    body_sha256 = f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"
    sections, duplicates = _extract_sections(body)
    texts = [body]
    if linked_issue_body:
        texts.append(linked_issue_body)
    is_safety_sensitive = _is_safety_sensitive(changed_paths, texts)
    errors: list[ValidationError] = []
    errors.extend(_validate_lp052(body, sections))
    errors.extend(_validate_lp054(body, duplicates))
    errors.extend(_validate_lp053(body, sections))
    errors.extend(_validate_lp050(body, sections, schema_decision_override))
    errors.extend(_validate_lp051(body, sections, is_safety_sensitive))
    errors.extend(_validate_lp055(body, sections, is_safety_sensitive))
    errors.extend(_validate_lp056(body, sections, is_safety_sensitive))
    errors.extend(_validate_safety_claims_v1_yaml_contract(body, sections))
    errors.extend(_validate_lp057(body, sections, linked_issue, reference_policy))
    errors.extend(_validate_lp058(body, changed_paths))
    errors.extend(_validate_lp059(body, linked_issue))
    return ValidationResult("loop_body_lint/v1", "pr", body_sha256, "fail" if errors else "pass", errors)


def _error_to_dict(error: ValidationError) -> dict[str, object]:
    return asdict(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate GitHub PR body against LOOP_PROTOCOL rules")
    parser.add_argument("--body-file", required=True, type=str)
    parser.add_argument("--changed-paths-file", type=str, default="")
    parser.add_argument("--linked-issue", required=True, type=int)
    parser.add_argument(
        "--linked-issue-body-file",
        type=str,
        default="",
        help="linked Issue body file（利用可能な場合のみ。safety-applicability minimum floor の入力）",
    )
    parser.add_argument(
        "--reference-facts-file",
        type=str,
        default="",
        help="reference policy facts JSON（供給時は LP057 が単一 evaluator の結果に従う。Issue #2878）",
    )
    parser.add_argument(
        "--native-close-facts-file",
        type=str,
        default="",
        help="native auto-close risk facts JSON（--evaluate-native-auto-close-risk の入力。Issue #2878）",
    )
    parser.add_argument(
        "--evaluate-native-auto-close-risk",
        action="store_true",
        help=(
            "nonclosing_required の merge 前に本文以外の自動 close 経路"
            "（native relation / 採用される merge message）を検査し JSON を出す（Issue #2878）"
        ),
    )
    parser.add_argument(
        "--evaluate-reference-policy",
        action="store_true",
        help="lint を実行せず reference policy evaluator の JSON object だけを stdout へ出す（Issue #2878）",
    )
    args = parser.parse_args(argv)
    try:
        body_bytes = Path(args.body_file).read_bytes()
    except OSError as exc:
        print(f"ERROR: Cannot read body file: {exc}", file=sys.stderr)
        return 2
    if args.evaluate_native_auto_close_risk:
        risk = evaluate_native_auto_close_risk_from_files(
            body_bytes,
            args.linked_issue,
            args.reference_facts_file,
            args.linked_issue_body_file,
            args.native_close_facts_file,
        )
        print(json.dumps(risk, ensure_ascii=False))
        return 0
    if args.evaluate_reference_policy:
        evaluation = evaluate_reference_policy_from_files(
            body_bytes, args.linked_issue, args.reference_facts_file, args.linked_issue_body_file
        )
        print(json.dumps(evaluation, ensure_ascii=False))
        return 0
    try:
        body = Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Cannot read body file: {exc}", file=sys.stderr)
        return 2
    reference_policy = None
    if args.reference_facts_file:
        reference_policy = evaluate_reference_policy_from_files(
            body_bytes, args.linked_issue, args.reference_facts_file, args.linked_issue_body_file
        )
    try:
        changed_paths = _load_changed_paths(args.changed_paths_file or None)
    except OSError as exc:
        print(f"ERROR: Cannot read changed-paths file: {exc}", file=sys.stderr)
        return 2
    linked_issue_body = None
    if args.linked_issue_body_file:
        try:
            linked_issue_body = Path(args.linked_issue_body_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: Cannot read linked-issue-body file: {exc}", file=sys.stderr)
            return 2
    result = validate_pr_body(
        body,
        changed_paths,
        args.linked_issue,
        linked_issue_body=linked_issue_body,
        reference_policy=reference_policy,
    )
    print(json.dumps(
        {
            "schema": result.schema,
            "target": result.target,
            "body_sha256": result.body_sha256,
            "status": result.status,
            "errors": [_error_to_dict(error) for error in result.errors],
        },
        indent=2
    ))
    return 1 if result.status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
