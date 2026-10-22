#!/usr/bin/env python3
"""GitHub Issue body validator for LOOP_PROTOCOL.

Validates Issue body against rule LP001-LP030 and returns JSON-formatted errors.
Used as a pre-write hook in create_issue_txn.py.

Exit codes:
  0: validation pass (no errors)
  1: validation fail (errors returned in JSON)
  2: internal error or CLI usage error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import yaml
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import prose_boundary_policy as _pbp

# Path to ISSUE_TEMPLATE directory (relative to repo root, resolved at runtime)
# __file__ is at: <repo>/.claude/skills/create-issue/scripts/validate_issue_body.py
# parents: [0]=scripts, [1]=create-issue, [2]=skills, [3]=.claude, [4]=<repo root>
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ISSUE_TEMPLATE_DIR = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE"

# Existing-issue readiness is an independently versioned compatibility
# contract. It intentionally does not read ISSUE_TEMPLATE/parent.yml: a
# template edit must not silently rewrite requirements for an open Issue.
EXISTING_ISSUE_READINESS_V1 = "existing_issue_readiness_v1"
_EXISTING_ISSUE_READINESS_V1_PARENT_SECTIONS = (
    "Machine-Readable Contract",
    "Summary",
    "Goal",
    "Desired Destination",
    "Current Validated Scope",
    "Decisions Fixed",
    "Quality Decision Record",
    "Parent Closure Rule",
    "Child Issues",
    "Remaining Parent Gaps",
    "Phase Handoff Contract",
    "Acceptance Criteria",
)

_SHARED_VC_SYNTAX_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
if str(_SHARED_VC_SYNTAX_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_VC_SYNTAX_DIR))

from vc_contract_syntax import (
    parse_ac_marker_line,
    parse_preflight_scope_marker_line,
    parse_verification_commands_section as _parse_vc_section,
)

# #1135: shared, section-bound, strict MRC parser (C12 と共有)
from mrc_contract_parser import (  # noqa: E402
    parse_machine_readable_contract,
    REASON_MISSING as MRC_REASON_MISSING,
    REASON_NO_FENCE as MRC_REASON_NO_FENCE,
    REASON_MULTIPLE_FENCES as MRC_REASON_MULTIPLE_FENCES,
    REASON_MULTIPLE_SECTIONS as MRC_REASON_MULTIPLE_SECTIONS,
    REASON_YAML_ERROR as MRC_REASON_YAML_ERROR,
    REASON_DUPLICATE_KEY as MRC_REASON_DUPLICATE_KEY,
    REASON_ROOT_NOT_MAPPING as MRC_REASON_ROOT_NOT_MAPPING,
)


# =============================================================================
# Type definitions
# =============================================================================

@dataclass(frozen=True)
class ValidationError:
    """A single validation error with metadata."""
    rule_id: str
    severity: Literal["error", "warning"]
    section: str
    line_start: int
    line_end: int
    message: str
    minimal_context: list[str]
    context_truncated: bool
    fix_hint: str = ""
    autofixable: bool = False
    expected: list[str] | None = None  # For LP010
    actual: list[str] | None = None    # For LP010


@dataclass(frozen=True)
class ValidationResult:
    """Complete validation result."""
    schema: str
    target: str
    body_sha256: str
    status: Literal["pass", "fail"]
    errors: list[ValidationError]


# =============================================================================
# Validation Rules
# =============================================================================

def _extract_section(body: str, section_name: str) -> tuple[str, int, int] | None:
    """Extract section content and line numbers.

    Returns (content, start_line, end_line) or None if not found.
    Line numbers are 1-indexed.
    """
    lines = body.split('\n')
    pattern = re.compile(rf'^##\s+{re.escape(section_name)}\s*$', re.IGNORECASE)

    start_idx = None
    for i, line in enumerate(lines):
        if pattern.match(line):
            start_idx = i
            break

    if start_idx is None:
        return None

    # Find next section or end of document
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if re.match(r'^##\s+', lines[i]):
            end_idx = i
            break

    # Extract content (skip header line)
    content = '\n'.join(lines[start_idx + 1:end_idx])
    return content.strip(), start_idx + 1, end_idx - 1


def _extract_sections(body: str) -> dict[str, tuple[str, int, int]]:
    """Extract all markdown sections with their line ranges."""
    sections = {}
    lines = body.split('\n')

    for i, line in enumerate(lines):
        if re.match(r'^##\s+', line):
            section_name = re.sub(r'^##\s+', '', line).strip()
            # Find content until next section
            content_start = i + 1
            content_end = len(lines)
            for j in range(i + 1, len(lines)):
                if re.match(r'^##\s+', lines[j]):
                    content_end = j
                    break

            content = '\n'.join(lines[content_start:content_end]).strip()
            sections[section_name] = (content, i + 1, content_end)

    return sections


def _get_context_lines(
    body: str,
    start_line: int,
    end_line: int,
    max_lines: int = 5,
    max_bytes: int = 2048
) -> tuple[list[str], bool]:
    """Extract context lines around the error range.

    Returns (context_lines, truncated_flag).
    Lines are 1-indexed.
    Both max_lines and max_bytes limits are enforced.
    """
    lines = body.split('\n')

    # Clamp to actual line count
    start = max(0, start_line - 1)
    end = min(len(lines), end_line)

    context = lines[start:end][:max_lines]  # First limit to max_lines

    # Then enforce byte limit
    truncated = False
    result = []
    total_bytes = 0

    for line in context:
        encoded = line.encode('utf-8')
        # Account for newline separator (1 byte)
        line_with_newline_cost = len(encoded) + 1

        if total_bytes + line_with_newline_cost > max_bytes:
            # This line would exceed limit
            if total_bytes == 0:
                # First line is too long, truncate it
                remaining = max_bytes
                truncated_line = encoded[:remaining].decode('utf-8', errors='ignore')
                result.append(truncated_line)
            truncated = True
            break

        result.append(line)
        total_bytes += line_with_newline_cost

    return result, truncated


# #1704: Recognises "-"/"*"/"+" task-list bullet markers (not only
# hyphen), 1-4 spaces between the marker and the checkbox, and ONLY a
# GFM-valid checkbox state ("[ ]"/"[x]"/"[X]", nothing else -- PR #1717
# review B1). Anchored to the start of the line (up to 3 leading spaces,
# matching GFM list-marker indentation, not arbitrary leading whitespace)
# so that an "AC<N>"-shaped token that merely appears in prose, a URL, a
# filename, inline code, or fenced code within the same section is never
# mistaken for an AC *definition* line (Issue #1704 AC1/AC4). AC numbers
# are constrained to ASCII digits ([0-9]+, not \d+) so full-width digit
# forms (e.g. "AC１２") are never extracted (PR #1717 review B1).
_AC_DEFINITION_LINE_RE = re.compile(
    r"^ {0,3}[-*+] {1,4}\[[ xX]\][ \t]+AC([0-9]+)\b"
)


def _iter_non_fenced_lines(content: str):
    """Yield (line) for every line in `content` that is NOT inside a
    fenced code block. Delegates fence detection to the shared,
    GFM-compliant `prose_boundary_policy.iter_markdown_blocks()` SSOT
    (tilde fences, 4-backtick fences containing literal 3-backtick lines,
    fence-length/char-type matching, indented code blocks, etc.) instead
    of a bespoke ``` toggle -- PR #1717 review B2. Used so AC-shaped
    tokens inside fenced code samples are never extracted as AC
    definitions (#1704 AC4).
    """
    for block_text, block_kind in _pbp.iter_markdown_blocks(content):
        if block_kind != _pbp.BLOCK_KIND_CODE_FENCE:
            yield from block_text.splitlines()


def _extract_ac_numbers(body: str) -> set[str]:
    """Extract AC numbers from 'Acceptance Criteria' section.

    #1704: recognises "-"/"*"/"+" bullet markers (mixed-bullet bodies no
    longer produce false negatives), and never extracts an "AC<N>"-shaped
    token that appears in prose, a URL, a filename, inline code, or fenced
    code rather than at the start of a checkbox list-item line.
    """
    section_info = _extract_section(body, "Acceptance Criteria")
    if not section_info:
        return set()

    content, _, _ = section_info
    numbers: set[str] = set()
    for line in _iter_non_fenced_lines(content):
        m = _AC_DEFINITION_LINE_RE.match(line)
        if m:
            numbers.add(f"AC{m.group(1)}")
    return numbers


def _extract_vc_ac_numbers(body: str) -> set[str]:
    """Extract AC numbers referenced in Verification Commands section.

    #993: Uses shared parse_verification_commands_section() parser.
    Recognises: bare # ACN markers, grouped # AC2, AC3 markers (#814),
    and inline suffix $ command # ACN.
    """
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return set()

    content, _, _ = section_info
    parse_result = _parse_vc_section(content)
    return set(parse_result.ac_refs)


def _load_required_section_labels(kind: str) -> list[str]:
    """Load required section labels from ISSUE_TEMPLATE/<kind>.yml.

    Returns labels of fields with validations.required: true (excluding markdown type).
    Falls back to a minimal default set if the template file is not found.
    """
    template_path = _ISSUE_TEMPLATE_DIR / f"{kind}.yml"
    if not template_path.exists():
        # Fallback to static minimal set when template is not available
        return ["Acceptance Criteria", "Verification Commands", "Allowed Paths"]

    try:
        form = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return ["Acceptance Criteria", "Verification Commands", "Allowed Paths"]

    labels: list[str] = []
    for item in form.get("body", []):
        if item.get("type") == "markdown":
            continue
        if item.get("validations", {}).get("required") is True:
            label = item.get("attributes", {}).get("label", "").removesuffix("*").strip()
            if label:
                labels.append(label)

    # AC9 (#1346): authoring 経路 (create_issue_txn.py 経由の --kind implementation) では
    # ISSUE_TEMPLATE の validations.required フラグに関わらず Required Design References を
    # 必須化する。implementation.yml の required-design-references フィールドは AC7 (#1346) で
    # scope_signal_guard の template-driven required-section 回帰 (plan_refinement_loop.py の
    # resolve_issue_template 経由) を避けるため validations.required: false に設定されているが、
    # authoring 経路の必須化はこの関数で独立に担保する。contract_readiness_check.py
    # (既存 issue 向け、寛容な挙動) はこの関数を呼ばないため影響しない。
    if kind == "implementation" and "Required Design References" not in labels:
        labels.append("Required Design References")

    return labels


def _load_stop_condition_keywords(kind: str) -> list[str]:
    """Load stop condition keywords from ISSUE_TEMPLATE/<kind>.yml stop-conditions value field.

    Returns list of condition strings (without leading "- ") from the template default value.
    Returns empty list if template not found or stop-conditions field not present.
    """
    template_path = _ISSUE_TEMPLATE_DIR / f"{kind}.yml"
    if not template_path.exists():
        return []

    try:
        form = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []

    for item in form.get("body", []):
        if item.get("id") == "stop-conditions":
            value = item.get("attributes", {}).get("value", "")
            conditions = []
            for line in value.splitlines():
                line = line.strip()
                if line.startswith("- "):
                    conditions.append(line[2:].strip())
            return conditions

    return []


def _validate_lp001_missing_required_section(
    body: str,
    kind: str | None = None,
    validation_profile: str | None = None,
) -> list[ValidationError]:
    """LP001: Detect missing required sections.

    When kind is provided, loads required sections dynamically from ISSUE_TEMPLATE/<kind>.yml.
    Falls back to a minimal static set when kind is None or template is not found.
    """
    if validation_profile == EXISTING_ISSUE_READINESS_V1 and kind == "parent":
        required_sections = list(_EXISTING_ISSUE_READINESS_V1_PARENT_SECTIONS)
    elif kind:
        required_sections = _load_required_section_labels(kind)
    else:
        required_sections = [
            "Acceptance Criteria",
            "Verification Commands",
            "Allowed Paths"
        ]

    sections = _extract_sections(body)
    errors = []

    for section_name in required_sections:
        if section_name not in sections:
            errors.append(ValidationError(
                rule_id="LP001",
                severity="error",
                section="(global)",
                line_start=1,
                line_end=1,
                message=f"Missing required section: {section_name}",
                minimal_context=["(Section not found)"],
                context_truncated=False,
                fix_hint=f"Add '## {section_name}' section to the Issue body.",
                autofixable=False
            ))

    return errors


def _validate_lp002_invalid_machine_readable_contract(body: str) -> list[ValidationError]:
    """LP002: Detect invalid Machine-Readable Contract YAML."""
    section_info = _extract_section(body, "Machine-Readable Contract")
    if not section_info:
        # LP002 not applicable if no contract section exists
        return []

    content, start_line, end_line = section_info

    # #1135 P0/P1b: parse the MRC strictly via the shared, section-bound parser
    # (exactly one ```yaml fence, duplicate-key rejecting, mapping root). C12
    # (check_issue_contract.py) shares this parser to avoid a parser differential.
    mrc_result = parse_machine_readable_contract(body)
    if not mrc_result.ok:
        if mrc_result.reason == MRC_REASON_MISSING:
            return []
        context, trunc = _get_context_lines(body, start_line, end_line)
        _lp002_msg = {
            MRC_REASON_NO_FENCE: "Machine-Readable Contract block must use ```yaml ... ``` fence",
            MRC_REASON_MULTIPLE_FENCES: (
                "Machine-Readable Contract section must contain exactly one ```yaml ... ``` fence"
            ),
            MRC_REASON_MULTIPLE_SECTIONS: "Body must contain exactly one ## Machine-Readable Contract section",
            MRC_REASON_YAML_ERROR: "Machine-Readable Contract YAML syntax error",
            MRC_REASON_DUPLICATE_KEY: f"Machine-Readable Contract has a duplicate key: {mrc_result.duplicate_key}",
            MRC_REASON_ROOT_NOT_MAPPING: "Machine-Readable Contract YAML must be a dictionary",
        }.get(mrc_result.reason, f"Machine-Readable Contract invalid ({mrc_result.reason})")
        return [ValidationError(
            rule_id="LP002",
            severity="error",
            section="Machine-Readable Contract",
            line_start=start_line,
            line_end=end_line,
            message=_lp002_msg,
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Ensure exactly one valid ```yaml ... ``` contract block with unique keys.",
            autofixable=False
        )]

    data = mrc_result.data

    # Parse and validate YAML
    errors = []

    # Check for required fields
    required_contract_fields = [
        "contract_schema_version",
        "issue_kind"
    ]

    for field in required_contract_fields:
        if field not in data:
            context, trunc = _get_context_lines(body, start_line, end_line)
            errors.append(ValidationError(
                rule_id="LP002",
                severity="error",
                section="Machine-Readable Contract",
                line_start=start_line,
                line_end=end_line,
                message=f"Machine-Readable Contract missing required field: {field}",
                minimal_context=context,
                context_truncated=trunc,
                fix_hint=f"Add '{field}: <value>' to YAML contract block.",
                autofixable=False
            ))

    return errors


# #1704 AC2 / PR #1717 review B3: the previous "vacuous empty-set match"
# guard (`_has_checkbox_shaped_lines()` matching ANY bullet marker) was
# itself over-broad -- it matched non-list-marker lines such as Markdown
# table rows (e.g. "| [ ] | unchecked |"), producing an
# expected=[]/actual=[] contradictory mismatch even when both the AC and
# VC sections genuinely define zero ACs. With `_AC_DEFINITION_LINE_RE`
# now strictly GFM-grammar-bound (B1), the original false-negative this
# guard was defending against (an all-asterisk/all-plus AC section
# silently extracting to an empty set) no longer occurs, so the guard is
# removed rather than patched -- an empty/empty match is always a
# legitimate pass.
def _validate_lp010_ac_vc_mismatch(body: str) -> list[ValidationError]:
    """LP010: Detect mismatch between AC and VC numbers."""
    ac_numbers = _extract_ac_numbers(body)
    vc_numbers = _extract_vc_ac_numbers(body)

    if ac_numbers == vc_numbers:
        return []

    # Find which section to report error on
    vc_section_info = _extract_section(body, "Verification Commands")
    if not vc_section_info:
        return []

    _, start_line, end_line = vc_section_info
    context, trunc = _get_context_lines(body, start_line, end_line)

    missing_in_vc = ac_numbers - vc_numbers
    extra_in_vc = vc_numbers - ac_numbers

    message = "AC ⇔ VC number set mismatch"
    if missing_in_vc:
        message += f" (missing in VC: {', '.join(sorted(missing_in_vc))})"
    if extra_in_vc:
        message += f" (extra in VC: {', '.join(sorted(extra_in_vc))})"

    return [ValidationError(
        rule_id="LP010",
        severity="error",
        section="Verification Commands",
        line_start=start_line,
        line_end=end_line,
        message=message,
        minimal_context=context,
        context_truncated=trunc,
        fix_hint="Ensure each AC has exactly one corresponding VC comment.",
        autofixable=False,
        expected=sorted(ac_numbers),
        actual=sorted(vc_numbers)
    )]


def _validate_lp011_verification_command_format(body: str) -> list[ValidationError]:
    """LP011: Detect invalid Verification Commands format.

    #993: Uses shared parse_verification_commands_section() to check for bash fences.
    """
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info

    # Use shared parser to detect bash fence presence
    parse_result = _parse_vc_section(content)
    if not parse_result.has_bash_fence:
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP011",
            severity="error",
            section="Verification Commands",
            line_start=start_line,
            line_end=end_line,
            message="Verification Commands must use ```bash ... ``` fenced blocks",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Wrap all commands in ```bash ... ``` code fence.",
            autofixable=False
        )]

    return []


def _validate_lp012_rg_encoding_flag(body: str) -> list[ValidationError]:
    """LP012: Detect misuse of rg -E flag.

    The -E flag in ripgrep is for --encoding (specifying file encoding),
    not for ERE like grep -E. Combining rg with -E (encoding flag) is error-prone.
    """
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    errors = []
    current_line = start_line

    for line in lines:
        # Skip pure comment lines or empty lines
        if line.strip().startswith('#') or not line.strip():
            current_line += 1
            continue

        # Check if this is an executable command line (not a comment)
        if 'rg' in line:
            try:
                # Remove trailing # AC<N> marker before tokenizing
                cleaned = re.sub(r'\s+#\s*AC\d+\s*:?\s*$', '', line)
                tokens = shlex.split(cleaned, posix=True)

                # Check for 'rg' command with '-E' token
                if tokens and tokens[0] == 'rg' and '-E' in tokens:
                    context, trunc = _get_context_lines(body, current_line, current_line)
                    errors.append(ValidationError(
                        rule_id="LP012",
                        severity="error",
                        section="Verification Commands",
                        line_start=current_line,
                        line_end=current_line,
                        message="rg -E flag (encoding) should not be used. Use rg -P for pattern matching instead.",
                        minimal_context=context,
                        context_truncated=trunc,
                        fix_hint="Replace 'rg -E' with 'rg -P' for extended regex patterns.",
                        autofixable=False
                    ))
            except ValueError:
                # shlex.split failed - not a valid command, skip
                pass

        current_line += 1

    return errors


def _validate_lp013_deletion_negative_grep(body: str) -> list[ValidationError]:
    """LP013: Detect deletion check without explicit literal targets."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    errors = []
    current_line = start_line

    for line in lines:
        if 'grep -v' in line or 'rg -v' in line:
            # Check if there's an explicit literal target (e.g., test -f, grep -q)
            if not any(x in line for x in ['test -f', 'test -d', 'grep -q', 'rg -q']):
                context, trunc = _get_context_lines(body, current_line, current_line)
                errors.append(ValidationError(
                    rule_id="LP013",
                    severity="warning",
                    section="Verification Commands",
                    line_start=current_line,
                    line_end=current_line,
                    message="Negative grep (-v) without explicit literal target may be ambiguous",
                    minimal_context=context,
                    context_truncated=trunc,
                    fix_hint="Add explicit file/pattern check before using grep -v.",
                    autofixable=False
                ))

        current_line += 1

    return errors


def _validate_lp014_markdown_backtick_grep(body: str) -> list[ValidationError]:
    """LP014: Detect grep on markdown backticks (common mistake)."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    errors = []
    current_line = start_line

    for line in lines:
        # Check for grep/rg used directly without capturing the command block first
        if any(x in line for x in ['grep', 'rg']) and '```' in line:
            context, trunc = _get_context_lines(body, current_line, current_line)
            errors.append(ValidationError(
                rule_id="LP014",
                severity="warning",
                section="Verification Commands",
                line_start=current_line,
                line_end=current_line,
                message="Grep/rg on markdown backticks may fail on literal backtick characters",
                minimal_context=context,
                context_truncated=trunc,
                fix_hint="Extract command content before grepping.",
                autofixable=False
            ))

        current_line += 1

    return errors


def _validate_lp015_baseline_vc_heading_only(body: str) -> list[ValidationError]:
    """LP015: Detect baseline VC that matches only heading (broad match)."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info

    # Check for commands that only match section headings (##)
    if 'grep' in content and '##' in content:
        # Syntactic broad-match detection
        if re.search(r'grep.*##', content):
            context, trunc = _get_context_lines(body, start_line, end_line)
            return [ValidationError(
                rule_id="LP015",
                severity="warning",
                section="Verification Commands",
                line_start=start_line,
                line_end=end_line,
                message="VC grep on '##' may match heading markers only (broad match)",
                minimal_context=context,
                context_truncated=trunc,
                fix_hint="Be more specific in grep pattern to avoid matching just headers.",
                autofixable=False
            )]

    return []


def _validate_lp020_runtime_verification_incomplete(
    body: str,
    *,
    require_reason: bool = False,
) -> list[ValidationError]:
    """LP020: Detect incomplete Runtime Verification Applicability."""
    section_info = _extract_section(body, "Runtime Verification Applicability")
    if not section_info:
        # If section exists, check completeness
        return []

    content, start_line, end_line = section_info

    # Check for decision field
    if not re.search(r'^\s*-?\s*decision:\s*(not_applicable|immediate|deferred)', content, re.MULTILINE):
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP020",
            severity="error",
            section="Runtime Verification Applicability",
            line_start=start_line,
            line_end=end_line,
            message="Runtime Verification Applicability must include 'decision' field",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Add 'decision: not_applicable|immediate|deferred' to section.",
            autofixable=False
        )]

    if require_reason and not re.search(r'^\s*-?\s*reason:\s*\S+', content, re.MULTILINE):
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP020",
            severity="error",
            section="Runtime Verification Applicability",
            line_start=start_line,
            line_end=end_line,
            message="Runtime Verification Applicability must include a non-empty 'reason' field",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Add a concrete 'reason: ...' for the selected decision.",
            autofixable=False
        )]

    # If decision is 'deferred', check for required fields
    if re.search(r'decision:\s*deferred', content, re.IGNORECASE):
        required_deferred_fields = ['deferred_destination', 'deferred_verification_condition']
        errors = []
        for field in required_deferred_fields:
            if field not in content:
                context, trunc = _get_context_lines(body, start_line, end_line)
                errors.append(ValidationError(
                    rule_id="LP020",
                    severity="error",
                    section="Runtime Verification Applicability",
                    line_start=start_line,
                    line_end=end_line,
                    message=f"deferred decision requires '{field}' field",
                    minimal_context=context,
                    context_truncated=trunc,
                    fix_hint=f"Add '{field}' field when decision is deferred.",
                    autofixable=False
                ))
        return errors

    return []


def _validate_lp_vc_parser_errors(body: str) -> list[ValidationError]:
    """Convert VcParseError entries from shared parser to ValidationError.

    Severity mapping:
      - non_dollar_command             -> warning (backward compat, edit-issue autofix handles)
      - preceding_marker_with_inline_suffix -> warning
      - inline_backtick               -> error
      - compound_shell                -> error
      - colon_marker / suffixed_marker -> skip (LP016 handles)
      - unlabeled_fence               -> skip (LP011 handles via has_bash_fence)
    """
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []
    content, start_line, end_line = section_info

    parse_result = _parse_vc_section(content)

    SEVERITY_MAP = {
        "non_dollar_command": "warning",
        "preceding_marker_with_inline_suffix": "warning",
        "inline_backtick": "error",
        "compound_shell": "error",
    }
    SKIP_KINDS = {"colon_marker", "suffixed_marker", "unlabeled_fence"}

    errors: list[ValidationError] = []
    for err in parse_result.errors:
        if err.kind in SKIP_KINDS:
            continue
        severity = SEVERITY_MAP.get(err.kind)
        if severity is None:
            continue
        abs_line = start_line + (err.line_number or 1) - 1
        context, trunc = _get_context_lines(body, abs_line, abs_line)
        errors.append(ValidationError(
            rule_id="LP_VC_PARSER",
            severity=severity,
            section="Verification Commands",
            line_start=abs_line,
            line_end=abs_line,
            message=f"VC parse error ({err.kind}): {err.raw_line!r}",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint=err.fix_hint or "",
        ))
    return errors


def _validate_lp016_vc_ac_marker_with_description(body: str) -> list[ValidationError]:
    """LP016: Detect VC AC marker suffix variants that are not bare markers.

    Canonical form: `# AC1` only.
    Variants like `# AC1: desc`, `# AC1：desc`, `# AC1 - desc`,
    `# AC1 — desc`, `# AC1 desc` are rejected.

    #993: Colon/fullwidth-colon variants get a colon-specific fix_hint via
    shared VcParseResult (kind="colon_marker"). Other suffixes use the
    generic fix_hint.
    """
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info

    # Build a map from raw_line → colon-specific fix_hint using shared parser
    # (only for errors inside bash fences)
    colon_fix_hints: dict[str, str] = {}
    parse_result = _parse_vc_section(content)
    for pe in parse_result.errors:
        if pe.rule_id == "LP016" and pe.kind == "colon_marker":
            colon_fix_hints[pe.raw_line] = pe.fix_hint

    errors = []
    lines = body.split('\n')[start_line - 1:end_line]
    current_line = start_line
    for line in lines:
        label, is_valid = parse_ac_marker_line(line)
        if label is not None and not is_valid:
            stripped = line.strip()
            # Use colon-specific fix_hint if available, generic otherwise
            fix_hint = colon_fix_hints.get(stripped) or "Use bare '# AC<N>' marker syntax on its own line."
            context, trunc = _get_context_lines(body, current_line, current_line)
            errors.append(ValidationError(
                rule_id="LP016",
                severity="error",
                section="Verification Commands",
                line_start=current_line,
                line_end=current_line,
                message=(
                    "VC AC marker must be bare '# AC<N>' without suffix. "
                    f"Found: {stripped!r}"
                ),
                minimal_context=context,
                context_truncated=trunc,
                fix_hint=fix_hint,
                autofixable=False
            ))
        current_line += 1

    return errors


def _validate_lp018_vc_preflight_scope_value(body: str) -> list[ValidationError]:
    """LP018: Validate preflight-scope markers and reject malformed values."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, section_start, _ = section_info
    errors: list[ValidationError] = []

    in_bash = False

    for idx, raw_line in enumerate(content.split("\n"), start=section_start):
        line = raw_line.strip()

        if line.startswith("```"):
            fence = line.lstrip().lower()
            if not in_bash:
                if fence.startswith("```bash"):
                    in_bash = True
                continue
            else:
                in_bash = False
            continue

        if not in_bash:
            continue

        marker, _ = parse_preflight_scope_marker_line(raw_line)
        if marker is None:
            continue

        if marker not in ("pr_review_only", "runtime_only"):
            context, trunc = _get_context_lines(body, idx, idx)
            errors.append(ValidationError(
                rule_id="LP018",
                severity="error",
                section="Verification Commands",
                line_start=idx,
                line_end=idx,
                message=(
                    "Invalid # preflight-scope value. "
                    f"Allowed values are: {', '.join(['pr_review_only', 'runtime_only'])}. "
                    f"Found: {raw_line.strip()!r}"
                ),
                minimal_context=context,
                context_truncated=trunc,
                fix_hint=(
                    "Use '# preflight-scope: pr_review_only' or "
                    "'# preflight-scope: runtime_only' on its own line immediately "
                    "before a command."
                ),
                autofixable=False,
            ))

    return errors


def _validate_lp019_vc_preflight_scope_attached(body: str) -> list[ValidationError]:
    """LP019: preflight-scope must be attached to a command line in bash block."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, section_start, _ = section_info
    errors: list[ValidationError] = []

    for m in re.finditer(r"```bash[ \t]*\n(.*?)```", content, re.DOTALL):
        block_start_offset = content[:m.start()].count("\n") + section_start + 1
        block_lines = m.group(1).split("\n")

        for i, raw_line in enumerate(block_lines, start=block_start_offset):
            marker, _ = parse_preflight_scope_marker_line(raw_line)
            if marker is None:
                continue

            # marker must be followed by a command line in the same block
            next_index = i - block_start_offset + 1
            attached = False
            if 0 <= next_index < len(block_lines):
                next_line = block_lines[next_index].strip()
                if next_line and not next_line.startswith("#"):
                    attached = True

            if not attached:
                context, trunc = _get_context_lines(body, i, i)
                errors.append(ValidationError(
                    rule_id="LP019",
                    severity="error",
                    section="Verification Commands",
                    line_start=i,
                    line_end=i,
                    message=(
                        "# preflight-scope marker must be immediately followed by "
                        "a command inside a ```bash block."
                    ),
                    minimal_context=context,
                    context_truncated=trunc,
                    fix_hint="Move marker immediately above the target VC command.",
                    autofixable=False,
                ))

    return errors


def _validate_lp017_stop_conditions_incomplete(body: str, kind: str | None = None) -> list[ValidationError]:
    """LP017: Detect incomplete Stop Conditions section (kind-specific check).

    When kind is provided and template has a stop-conditions field, checks that
    each template-defined condition keyword appears somewhere in the body's
    Stop Conditions section (keyword match, not full-text equality).
    Only runs when kind is provided and template conditions are available.
    """
    if not kind:
        return []

    template_conditions = _load_stop_condition_keywords(kind)
    if not template_conditions:
        return []

    section_info = _extract_section(body, "Stop Conditions")
    if not section_info:
        # Missing section is already caught by LP001; skip here to avoid double-reporting
        return []

    content, start_line, end_line = section_info

    missing_conditions: list[str] = []
    for condition in template_conditions:
        # Keyword match: check if a significant portion of the condition appears in content
        # Use first ~30 chars as a keyword fingerprint (avoids minor wording drift)
        keyword = condition[:40].strip()
        if keyword and keyword not in content:
            missing_conditions.append(condition)

    if not missing_conditions:
        return []

    context, trunc = _get_context_lines(body, start_line, end_line)
    return [ValidationError(
        rule_id="LP017",
        severity="error",
        section="Stop Conditions",
        line_start=start_line,
        line_end=end_line,
        message=(
            f"Stop Conditions section is missing {len(missing_conditions)} of "
            f"{len(template_conditions)} required condition(s) from template. "
            f"Missing: {'; '.join(missing_conditions[:3])}"
            + (" ..." if len(missing_conditions) > 3 else "")
        ),
        minimal_context=context,
        context_truncated=trunc,
        fix_hint=(
            "Ensure all template-defined Stop Conditions are present in the section. "
            "Do not omit or shorten the standard 6 items."
        ),
        autofixable=False
    )]


def _extract_mrc_issue_kind(body: str) -> str | None:
    """Extract issue_kind from Machine-Readable Contract YAML block.

    Returns the issue_kind string if found and valid, or None.
    """
    section_info = _extract_section(body, "Machine-Readable Contract")
    if not section_info:
        return None

    content, _, _ = section_info
    yaml_match = re.search(r'```yaml\n(.*?)\n```', content, re.DOTALL)
    if not yaml_match:
        return None

    try:
        data = yaml.safe_load(yaml_match.group(1))
        if not isinstance(data, dict):
            return None
        if data.get("contract_schema_version") != "v1":
            return None
        issue_kind = data.get("issue_kind")
        if isinstance(issue_kind, str) and issue_kind.strip():
            return issue_kind.strip()
    except yaml.YAMLError:
        pass
    return None


# Implementation issue title prefix constants
_IMPLEMENTATION_TITLE_PREFIXES = ("実装:", "implement:")


def _validate_lp031_kind_mismatch(body: str, cli_kind: str | None) -> list[ValidationError]:
    """LP031 pre-check: Detect mismatch between MRC issue_kind and CLI --kind.

    If both MRC issue_kind and CLI --kind are present and they differ, fail before LP031.
    Returns errors if mismatch detected.
    """
    mrc_kind = _extract_mrc_issue_kind(body)

    if mrc_kind is None or cli_kind is None:
        return []

    if mrc_kind != cli_kind:
        return [ValidationError(
            rule_id="LP031",
            severity="error",
            section="Machine-Readable Contract",
            line_start=1,
            line_end=1,
            message=(
                f"issue_kind mismatch: MRC declares '{mrc_kind}' but CLI --kind is '{cli_kind}'. "
                "Resolve the conflict before proceeding."
            ),
            minimal_context=[f"MRC issue_kind: {mrc_kind}", f"CLI --kind: {cli_kind}"],
            context_truncated=False,
            fix_hint=(
                f"Either update the MRC issue_kind to '{cli_kind}' or "
                f"change --kind to '{mrc_kind}' to match the body."
            ),
            autofixable=False
        )]

    return []


def _validate_lp031_implementation_title_prefix(
    body: str,
    title: str | None,
    effective_kind: str | None,
) -> list[ValidationError]:
    """LP031: For implementation kind issues, title must start with '実装:' or 'implement:'.

    Only runs when effective_kind == 'implementation' and title is provided.
    Returns errors if title prefix is non-compliant.
    """
    if effective_kind != "implementation":
        return []

    if title is None:
        return []

    if title.startswith(_IMPLEMENTATION_TITLE_PREFIXES):
        return []

    return [ValidationError(
        rule_id="LP031",
        severity="error",
        section="(global)",
        line_start=1,
        line_end=1,
        message=(
            f"[LP031] implementation issue title must start with '実装:' or 'implement:'. "
            f"Got: {title!r}"
        ),
        minimal_context=[f"title: {title!r}"],
        context_truncated=False,
        fix_hint="Change title to start with '実装: ' or 'implement: '.",
        autofixable=False
    )]


def _validate_lp030_forbidden_authoring_doc_path(body: str) -> list[ValidationError]:
    """LP030: Detect reference to forbidden authoring doc path."""
    forbidden_paths = ["docs/dev/body-authoring.md"]

    errors = []
    lines = body.split('\n')

    for i, line in enumerate(lines, 1):
        for path in forbidden_paths:
            if path in line:
                context, trunc = _get_context_lines(body, i, i)
                errors.append(ValidationError(
                    rule_id="LP030",
                    severity="error",
                    section="(global)",
                    line_start=i,
                    line_end=i,
                    message=f"Reference to forbidden path: {path}",
                    minimal_context=context,
                    context_truncated=trunc,
                    fix_hint=f"Remove reference to {path}.",
                    autofixable=False
                ))

    return errors


# =============================================================================
# Main validation dispatcher
# =============================================================================

def validate_issue_body(
    body: str,
    kind: str | None = None,
    title: str | None = None,
    validation_profile: str | None = None,
) -> ValidationResult:
    """Run all validation rules and return aggregated results.

    Args:
        body: The issue body text to validate.
        kind: Optional issue kind (CLI --kind). When provided,
              kind-specific rules (LP001 dynamic sections, LP017 Stop Conditions)
              load their requirements from the corresponding ISSUE_TEMPLATE file.
        title: Optional issue title. When provided, LP031 checks title prefix
               for implementation kind issues.

    effective_kind resolution:
        1. CLI kind (--kind) is the authoritative source for LP001/LP017 kind-specific checks.
        2. MRC issue_kind (from body) is extracted separately for LP031 mismatch detection.
        3. If both CLI --kind and MRC issue_kind are present and differ, LP031 reports mismatch.
        4. effective_kind (used for LP001/LP017) is only set when --kind is explicitly provided.
           MRC issue_kind alone does NOT activate kind-specific LP001/LP017 checks — this
           prevents regressions when validate_issue_body is called without --kind (e.g. from
           contract_readiness_check.py which does not forward --kind).
    """

    # Compute SHA256 of body
    body_bytes = body.encode('utf-8')
    body_sha256 = f"sha256:{hashlib.sha256(body_bytes).hexdigest()}"

    # effective_kind for LP001/LP017: only use CLI --kind (explicit caller intent).
    # MRC issue_kind is used only for LP031 mismatch and title prefix checks.
    effective_kind = kind

    # Run all validators
    all_errors: list[ValidationError] = []

    # LP031 mismatch check: must run before other LP031 checks
    kind_mismatch_errors = _validate_lp031_kind_mismatch(body, kind)
    all_errors.extend(kind_mismatch_errors)

    all_errors.extend(
        _validate_lp001_missing_required_section(
            body,
            kind=effective_kind,
            validation_profile=validation_profile,
        )
    )
    all_errors.extend(_validate_lp002_invalid_machine_readable_contract(body))
    all_errors.extend(_validate_lp010_ac_vc_mismatch(body))
    all_errors.extend(_validate_lp011_verification_command_format(body))
    all_errors.extend(_validate_lp012_rg_encoding_flag(body))
    all_errors.extend(_validate_lp013_deletion_negative_grep(body))
    all_errors.extend(_validate_lp014_markdown_backtick_grep(body))
    all_errors.extend(_validate_lp015_baseline_vc_heading_only(body))
    all_errors.extend(_validate_lp016_vc_ac_marker_with_description(body))
    all_errors.extend(_validate_lp_vc_parser_errors(body))
    all_errors.extend(_validate_lp018_vc_preflight_scope_value(body))
    all_errors.extend(_validate_lp019_vc_preflight_scope_attached(body))
    all_errors.extend(_validate_lp017_stop_conditions_incomplete(body, kind=effective_kind))
    all_errors.extend(
        _validate_lp020_runtime_verification_incomplete(
            body,
            require_reason=effective_kind == "implementation",
        )
    )
    all_errors.extend(_validate_lp030_forbidden_authoring_doc_path(body))

    # LP031 title prefix check: use MRC issue_kind when CLI --kind is absent,
    # so title prefix enforcement works even without explicit --kind.
    # (LP031 mismatch guard above already ran using CLI kind.)
    mrc_kind = _extract_mrc_issue_kind(body)
    lp031_kind = mrc_kind if mrc_kind is not None else kind
    if not kind_mismatch_errors:
        all_errors.extend(_validate_lp031_implementation_title_prefix(body, title, lp031_kind))

    # Determine overall status
    has_errors = any(e.severity == "error" for e in all_errors)
    status = "fail" if has_errors else "pass"

    return ValidationResult(
        schema="loop_body_lint/v1",
        target="issue",
        body_sha256=body_sha256,
        status=status,
        errors=all_errors
    )


# =============================================================================
# CLI and JSON serialization
# =============================================================================

def _error_to_dict(error: ValidationError) -> dict:
    """Convert ValidationError to JSON-serializable dict."""
    d = asdict(error)
    # Ensure all fields are present
    if d.get('expected') is None:
        del d['expected']
    if d.get('actual') is None:
        del d['actual']
    return d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate GitHub Issue body against LOOP_PROTOCOL rules"
    )
    parser.add_argument(
        "--body-file",
        type=str,
        help="Path to issue body file"
    )
    parser.add_argument(
        "--body",
        type=str,
        default="",
        help="Issue body text (used if --body-file not provided)"
    )
    parser.add_argument(
        "--kind",
        type=str,
        default=None,
        help=(
            "Issue kind (e.g. 'implementation'). "
            "--kind enables LP001/LP017 kind-specific checks: "
            "LP001 loads required sections from ISSUE_TEMPLATE/<kind>.yml, "
            "LP017 checks Stop Conditions against template-defined conditions. "
            "MRC issue_kind (from body) is used for LP031 title-prefix enforcement "
            "and mismatch detection only; it does NOT activate LP001/LP017 checks."
        )
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help=(
            "Issue title. When provided and kind is 'implementation', "
            "LP031 checks that title starts with '実装:' or 'implement:'."
        )
    )
    parser.add_argument(
        "--validation-profile",
        choices=[EXISTING_ISSUE_READINESS_V1],
        default=None,
        help=(
            "Versioned existing-issue readiness profile. It owns required "
            "sections and does not load ISSUE_TEMPLATE fields."
        ),
    )

    args = parser.parse_args(argv)

    # Read body
    body = ""
    if args.body_file:
        try:
            body = Path(args.body_file).read_text(encoding='utf-8')
        except OSError as exc:
            print(f"ERROR: Cannot read body file: {exc}", file=sys.stderr)
            return 2
    else:
        body = args.body

    if not body:
        print("ERROR: No body provided (--body or --body-file required)", file=sys.stderr)
        return 2

    # Validate
    result = validate_issue_body(
        body,
        kind=args.kind,
        title=args.title,
        validation_profile=args.validation_profile,
    )

    # Output JSON
    output = {
        "schema": result.schema,
        "target": result.target,
        "body_sha256": result.body_sha256,
        "status": result.status,
        "errors": [_error_to_dict(e) for e in result.errors]
    }

    print(json.dumps(output, indent=2))

    # Exit codes
    if result.status == "fail":
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main())
