#!/usr/bin/env python3
r"""repair_issue_contract.py

Deterministic, mutation-free repair pass for common Issue contract defects
that can be fixed without LLM rewriting.

Repairs performed:
1. escaped_code_fence  — Unescape ``\`\`\`bash`` / ``\`\`\`yaml`` / ``\`\`\``` in the
                         ``## Machine-Readable Contract`` section (CommonMark/GitHub
                         fenced code blocks in backtick-escaped YAML strings).
2. runtime_only_command — Annotate allowlist-outside runtime-only commands
                          (e.g. ``pnpm test:e2e``) with
                          ``# baseline-expect: deferred`` / ``# preflight-scope: pr_review_only``.
3. deferred_to_pr_review_only — Replace ONLY the annotation line of a pytest VC that
                          misuses ``# baseline-expect: deferred`` as a stand-in for
                          ``# preflight-scope: pr_review_only`` (Issue #3020). Narrow,
                          real-producer-classification-based conditions; the command
                          line is never changed.

Design:
- dry-run by default (no file written unless --apply is given)
- idempotent (running twice produces the same hash)
- pure Python string processing (no subprocess / shell)
- escaped_code_fence repair is limited to ## Machine-Readable Contract section
- allowlist-outside command repair is limited to ## Verification Commands section
- denylist commands (curl, rm, bash -c, node -e, etc.) are NOT repaired
- pnpm typecheck/lint/test/build are NOT marked as deferred

Exit codes:
  0 - repair ran without error (dry-run or applied)
  1 - input error or internal failure

Output (stdout): JSON in repair_issue_contract/v1 schema.

Usage:
    python3 repair_issue_contract.py --body-file <path> [--apply] [--out-file <path>]
    python3 repair_issue_contract.py --body-file <path> --apply --out-file repaired.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import re
import stat
import sys
import time
from pathlib import Path
from typing import Optional

# Issue #995 P0-5 fix_delta: reuse the canonical, section-bound,
# duplicate-key-rejecting Machine-Readable Contract parser (Issue #1135 SSOT)
# instead of an ad-hoc regex that fails open (returns None -> callers silently
# skip required-key checks) on malformed/duplicate-key/multi-fence input.
_CREATE_ISSUE_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "create-issue" / "scripts"
if str(_CREATE_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS_DIR))
try:
    from mrc_contract_parser import parse_machine_readable_contract as _canonical_parse_mrc
except ImportError:  # pragma: no cover - defensive fallback (fail-closed, not fail-open)
    _canonical_parse_mrc = None

# Issue #2783: shared no-path marker predicate (`scripts/agent-ops/`). Pure
# policy library only -- not a grammar library import.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_OPS_DIR = _REPO_ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))
from allowed_paths_policy import is_no_path_marker  # noqa: E402

# Issue #3020: reuse the shared VC annotation grammar that the real producer
# (baseline_vc_preflight.py) itself consumes, so the deferred -> pr_review_only
# repair decides on exactly the same preceding-line / contiguous-block semantics.
# Fail-closed: when the grammar module is unavailable the repair pass is a no-op.
_VC_SYNTAX_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
if str(_VC_SYNTAX_DIR) not in sys.path:
    sys.path.insert(0, str(_VC_SYNTAX_DIR))
try:
    from vc_contract_syntax import (  # noqa: E402
        extract_baseline_expect_annotation as _extract_baseline_expect_annotation,
        parse_preflight_scope_marker_line as _parse_preflight_scope_marker_line,
    )
except ImportError:  # pragma: no cover - defensive fallback (fail-closed, not fail-open)
    _extract_baseline_expect_annotation = None
    _parse_preflight_scope_marker_line = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA = "repair_issue_contract/v1"

# ---------------------------------------------------------------------------
# repair_action disposition (Issue #2016)
#
# `confidence` alone is diagnostic metadata, not an authorization signal
# (OWNER adversarial review on Issue #2016, P1-4). This module computes a
# single versioned, closed-enum `repair_action.disposition` from ALL
# `repairs[]` records so that downstream consumers (run_refinement_preflight.py)
# never have to re-derive safety semantics from raw repair records.
# ---------------------------------------------------------------------------

REPAIR_ACTION_SCHEMA_VERSION = "repair_action/v1"
REPAIR_ACTION_POLICY_VERSION = "deterministic-issue-repair/v1"

DISPOSITION_AUTO_APPLY_SAFE = "auto_apply_safe"
DISPOSITION_HUMAN_REVIEW_REQUIRED = "human_review_required"
DISPOSITION_INFORMATIONAL = "informational"
DISPOSITION_INVALID_PAYLOAD = "invalid_payload"

# Non-mutating diagnostic kinds: original == repaired always, these never
# affect the aggregate disposition.
_INFORMATIONAL_REPAIR_KINDS = frozenset({"non_target_fence"})

# Mutating kinds that MAY be auto-applied, but only when the record also
# carries `confidence: "high"`. Missing confidence on one of these kinds is
# treated as a missing safety classification (human_review_required), not
# as an implicit high-confidence grant.
_SAFE_MUTATING_KINDS_REQUIRE_HIGH_CONFIDENCE = frozenset({
    "move_inline_baseline_expect_to_preceding_line",
    "insert_baseline_expect_fail",
    # Issue #3020: annotation-line-only replacement of a misused
    # `baseline-expect: deferred` on an in-scope pytest VC.
    "deferred_to_pr_review_only",
})

# Known mutating kinds that are never auto-safe regardless of confidence
# (kept distinct from "unknown kind" purely for reason_code specificity).
_KNOWN_NON_AUTO_SAFE_MUTATING_KINDS = frozenset({
    "runtime_only_command",
    "escaped_code_fence",
})


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    a_start, a_end = a
    b_start, b_end = b
    return a_start <= b_end and b_start <= a_end


def classify_repair_action(
    original_body_sha256: str,
    repaired_body_sha256: str,
    repairs: object,
) -> dict:
    """Classify all `repairs[]` records into a single versioned disposition.

    Disposition closed enum:
      auto_apply_safe        - >=1 known-safe mutating repair with
                                confidence: high, no unsafe/unknown/mixed/
                                overlapping records present.
      human_review_required  - unknown kind, missing safety classification,
                                a non-auto-safe mutating kind, safe/unsafe
                                mixed, or overlapping mutating repairs.
      informational           - no mutating repairs at all (empty repairs[],
                                or only non-mutating diagnostic records).
      invalid_payload         - `repairs` is not a list, or contains a
                                malformed (non-dict / missing "kind") record.

    Design note (P1-4 of the Issue #2016 adversarial review): this function
    deliberately does NOT use `all(r.get("confidence") == "high" for r in
    repairs)` because Python's `all()` returns True on an empty iterable,
    which would misclassify `repairs == []` as safe. The `repairs == []`
    and malformed-record cases are branched explicitly instead.
    """
    if not isinstance(repairs, list):
        return {
            "schema_version": REPAIR_ACTION_SCHEMA_VERSION,
            "policy_version": REPAIR_ACTION_POLICY_VERSION,
            "disposition": DISPOSITION_INVALID_PAYLOAD,
            "original_body_sha256": original_body_sha256,
            "repaired_body_sha256": repaired_body_sha256,
            "diagnostics_artifact": None,
            "candidate_body_artifact": None,
            "repair_kinds": [],
            "reason_codes": ["repairs_field_not_a_list"],
        }

    if not repairs:
        return {
            "schema_version": REPAIR_ACTION_SCHEMA_VERSION,
            "policy_version": REPAIR_ACTION_POLICY_VERSION,
            "disposition": DISPOSITION_INFORMATIONAL,
            "original_body_sha256": original_body_sha256,
            "repaired_body_sha256": repaired_body_sha256,
            "diagnostics_artifact": None,
            "candidate_body_artifact": None,
            "repair_kinds": [],
            "reason_codes": ["no_repairs_detected"],
        }

    mutating_kinds_seen: list[str] = []
    has_unsafe = False
    has_safe_mutating = False
    unsafe_reason_codes: set[str] = set()
    mutating_ranges: list[tuple[int, int]] = []

    for record in repairs:
        if not isinstance(record, dict) or "kind" not in record:
            has_unsafe = True
            unsafe_reason_codes.add("malformed_repair_record")
            continue

        kind = record.get("kind")

        if kind in _INFORMATIONAL_REPAIR_KINDS:
            continue  # non-mutating diagnostic record; does not affect disposition

        if kind not in mutating_kinds_seen:
            mutating_kinds_seen.append(kind)

        confidence = record.get("confidence")

        if kind in _SAFE_MUTATING_KINDS_REQUIRE_HIGH_CONFIDENCE and confidence == "high":
            has_safe_mutating = True
            start = record.get("line_start")
            end = record.get("line_end")
            if isinstance(start, int) and isinstance(end, int):
                mutating_ranges.append((start, end))
        elif kind in _SAFE_MUTATING_KINDS_REQUIRE_HIGH_CONFIDENCE:
            has_unsafe = True
            unsafe_reason_codes.add("missing_safety_classification")
        elif kind in _KNOWN_NON_AUTO_SAFE_MUTATING_KINDS:
            has_unsafe = True
            unsafe_reason_codes.add("non_auto_safe_repair_kind")
        else:
            has_unsafe = True
            unsafe_reason_codes.add("unknown_repair_kind")

    overlap_found = any(
        _ranges_overlap(mutating_ranges[i], mutating_ranges[j])
        for i in range(len(mutating_ranges))
        for j in range(i + 1, len(mutating_ranges))
    )

    if has_unsafe:
        disposition = DISPOSITION_HUMAN_REVIEW_REQUIRED
        reason_codes = sorted(unsafe_reason_codes)
    elif overlap_found:
        disposition = DISPOSITION_HUMAN_REVIEW_REQUIRED
        reason_codes = ["overlapping_repair"]
    elif has_safe_mutating:
        disposition = DISPOSITION_AUTO_APPLY_SAFE
        reason_codes = ["deterministic_body_author_fix_available"]
    else:
        # Only informational (non-mutating) records were present.
        disposition = DISPOSITION_INFORMATIONAL
        reason_codes = ["no_mutating_repair_detected"]

    return {
        "schema_version": REPAIR_ACTION_SCHEMA_VERSION,
        "policy_version": REPAIR_ACTION_POLICY_VERSION,
        "disposition": disposition,
        "original_body_sha256": original_body_sha256,
        "repaired_body_sha256": repaired_body_sha256,
        "diagnostics_artifact": None,
        "candidate_body_artifact": None,
        "repair_kinds": mutating_kinds_seen,
        "reason_codes": reason_codes,
    }


# Commands that are in the allowlist and must NOT be deferred/annotated.
# These are baseline regression gates.
_PNPM_GATE_COMMANDS = frozenset([
    "pnpm typecheck",
    "pnpm lint",
    "pnpm test",
    "pnpm build",
])

# Commands that are entirely unsafe for VC preflight and must NOT be auto-repaired.
# We leave these as-is so the human (or LLM) deals with them explicitly.
_DENYLIST_PREFIXES = (
    "curl",
    "wget",
    "rm ",
    "rm\t",
    "rm\n",
    "mv ",
    "cp ",
    "chmod",
    "chown",
    "touch",
    "bash -c",
    "bash\t",
    "sh -c",
    "node -e",
    "python3 -c",
    "python -c",
    "perl -e",
    "ruby -e",
    "sed -i",
    "tee",
)

# Runtime-only patterns: commands that run side effects or require a running server/browser.
# These are candidates for deferred annotation.
_RUNTIME_ONLY_PATTERNS = [
    re.compile(r"^pnpm\s+test:e2e\b"),
    re.compile(r"^pnpm\s+run\s+test:e2e\b"),
    re.compile(r"^npx\s+playwright\b"),
    re.compile(r"^npx\s+cypress\b"),
    re.compile(r"^playwright\b"),
    re.compile(r"^cypress\b"),
    re.compile(r"^pnpm\s+test:.*:e2e\b"),
    re.compile(r"^pnpm\s+run\s+test:.*:e2e\b"),
]

# Marker that indicates a command is already annotated (idempotency guard).
_ALREADY_ANNOTATED_RE = re.compile(
    r"#\s*(baseline-expect:|preflight-scope:)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Escaped code fence repair (## Machine-Readable Contract section only)
# ---------------------------------------------------------------------------

# Match the ## Machine-Readable Contract section header and capture its content
_MRC_SECTION_RE = re.compile(
    r"(^##\s+Machine-Readable Contract\s*$)(.+?)(?=^##|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Patterns for escaped fences that appear inside YAML/markdown string values:
# Typically rendered as \`\`\`bash or \`\`\`  or \\n\`\`\` etc.
# We detect: a line that is *entirely* a backslash-escaped fence opener or closer.
#
# CommonMark/GitHub safe fence forms that must NOT be touched:
#   - quadruple fence: ````bash ... ````
#   - tilde fence:     ~~~bash ... ~~~
_ESCAPED_FENCE_LINE_RE = re.compile(
    r"^(\\?`{3,})(bash|yaml|json|sh|)?\s*$",
    re.MULTILINE,
)


def _repair_escaped_code_fences(body: str) -> tuple[str, list[dict]]:
    r"""Repair escaped code fences inside the ## Machine-Readable Contract section.

    Only modifies lines that are **entirely** a backslash-escaped fence
    (e.g., ``\`\`\`bash``, ``\`\`\```) to unescaped equivalents.

    CommonMark/GitHub legal forms (quadruple fence, tilde fence) are NOT touched.

    Returns (repaired_body, repairs[])
    """
    repairs: list[dict] = []
    result = body

    def _replace_mrc_section(match: re.Match) -> str:
        header = match.group(1)
        section_body = match.group(2)
        repaired_section, section_repairs = _repair_section_fences(
            section_body, body_offset=match.start(2)
        )
        repairs.extend(section_repairs)
        return header + repaired_section

    result = _MRC_SECTION_RE.sub(_replace_mrc_section, result)
    return result, repairs


def _repair_section_fences(section: str, body_offset: int) -> tuple[str, list[dict]]:
    """Apply fence repair inside a section.  body_offset is the character offset
    of section start within the full body (used for line number computation).

    MAJOR 1 fix: Only target ``yaml`` opening fences (not bash/json/sh).
    Uses a state machine to track the current open fence so that closing fences
    inside a non-yaml block are not erroneously repaired.
    After repair, re-parse the MRC YAML to confirm structural validity.
    If YAML re-parse fails, the repair is rejected (original section returned).
    """
    repairs: list[dict] = []
    lines = section.split("\n")
    new_lines: list[str] = []

    # State machine: track current open fence language
    current_fence_lang: str | None = None  # None = outside fence

    for i, line in enumerate(lines):
        # Check for any escaped fence (with or without language label)
        m_escaped = re.match(r"^\\(`{3,})(\w*)\s*$", line)
        # Check for unescaped fence (to track state)
        m_unescaped = re.match(r"^`{3,}(\w*)\s*$", line)

        if m_escaped:
            lang = m_escaped.group(2)  # "" for unlabeled (closing or unlabeled opening)
            backticks = m_escaped.group(1)

            if current_fence_lang is None:
                # Opening escaped fence
                if lang == "yaml" or (lang == "" and current_fence_lang is None):
                    # Could be yaml opening or unlabeled opening.
                    # We only repair yaml opening fences.
                    # But unlabeled could be a closing fence (if we were inside a block)
                    # or an unlabeled opening — treat as yaml target only if lang == "yaml"
                    if lang == "yaml":
                        # yaml opening fence: repair it
                        unescaped = backticks + lang
                        body_lines_before = section[:section.find(line)].count("\n") if section.find(line) >= 0 else 0
                        line_start = body_lines_before + 1
                        repairs.append({
                            "kind": "escaped_code_fence",
                            "line_start": line_start,
                            "line_end": line_start,
                            "reason": "machine_readable_contract_fence_escaped",
                            "original": line,
                            "repaired": unescaped,
                        })
                        new_lines.append(unescaped)
                        current_fence_lang = "yaml"
                        continue
                    else:
                        # Unlabeled (could be yaml closing when we're not inside) - skip
                        # Or non-yaml opening - record as non_target_fence
                        repairs.append({
                            "kind": "non_target_fence",
                            "line_start": i + 1,
                            "line_end": i + 1,
                            "reason": "unlabeled_escaped_fence_outside_block",
                            "original": line,
                            "repaired": line,
                        })
                else:
                    # Non-yaml language opening fence: skip, record as non_target
                    repairs.append({
                        "kind": "non_target_fence",
                        "line_start": i + 1,
                        "line_end": i + 1,
                        "reason": f"non_yaml_fence_skipped: {lang}",
                        "original": line,
                        "repaired": line,
                    })
                    current_fence_lang = lang if lang else "__non_yaml__"
            else:
                # Inside a fence block: this is a closing fence
                if current_fence_lang == "yaml":
                    # Closing fence of yaml block: repair it
                    unescaped = backticks
                    body_lines_before = section[:section.find(line)].count("\n") if section.find(line) >= 0 else 0
                    line_start = body_lines_before + 1
                    repairs.append({
                        "kind": "escaped_code_fence",
                        "line_start": line_start,
                        "line_end": line_start,
                        "reason": "machine_readable_contract_fence_escaped",
                        "original": line,
                        "repaired": unescaped,
                    })
                    new_lines.append(unescaped)
                    current_fence_lang = None
                    continue
                else:
                    # Closing fence of non-yaml block: do not repair
                    repairs.append({
                        "kind": "non_target_fence",
                        "line_start": i + 1,
                        "line_end": i + 1,
                        "reason": "non_yaml_closing_fence_skipped",
                        "original": line,
                        "repaired": line,
                    })
                    current_fence_lang = None
        elif m_unescaped:
            # Track unescaped fence state (already-correct fences)
            lang = m_unescaped.group(1)
            if current_fence_lang is None:
                current_fence_lang = lang if lang else "__unlabeled__"
            else:
                current_fence_lang = None

        new_lines.append(line)

    repaired_section = "\n".join(new_lines)

    # MAJOR 1 fix: Re-parse MRC YAML after repair to confirm structural validity.
    # If the repaired section cannot be parsed as valid YAML, reject all repairs
    # (return the original section unchanged with empty repairs list).
    yaml_repairs = [r for r in repairs if r["kind"] == "escaped_code_fence"]
    if yaml_repairs:
        try:
            import yaml as _yaml
            yaml_block_re = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
            for yaml_match in yaml_block_re.finditer(repaired_section):
                yaml_content = yaml_match.group(1)
                _yaml.safe_load(yaml_content)
        except Exception:
            # YAML parse failed after repair: reject repair, return original section unchanged
            return section, []

    # Return escaped_code_fence repairs first, then non_target_fence (informational)
    return repaired_section, (
        [r for r in repairs if r["kind"] == "escaped_code_fence"] +
        [r for r in repairs if r["kind"] == "non_target_fence"]
    )



# ---------------------------------------------------------------------------
# Runtime-only command annotation repair (## Verification Commands section only)
# ---------------------------------------------------------------------------

_VC_SECTION_RE = re.compile(
    r"(^##\s+Verification Commands\s*$)(.+?)(?=^##|\Z)",
    re.MULTILINE | re.DOTALL,
)

_COMMAND_LINE_RE = re.compile(r"^\$\s+(.+)$")
_PREFLIGHT_SCOPE_ALREADY_RE = re.compile(
    r"^\s*#\s*preflight-scope:\s*(pr_review_only|runtime_only)(\s.*)?$"
)
_BASELINE_EXPECT_ALREADY_RE = re.compile(
    r"^\s*#\s*baseline-expect:\s*(deferred|pass|fail)\s*$"
)


def _is_denylist_command(cmd: str) -> bool:
    """Return True if command starts with a denylist prefix (not auto-repairable)."""
    stripped = cmd.strip()
    for prefix in _DENYLIST_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def _is_pnpm_gate(cmd: str) -> bool:
    """Return True if command is a pnpm regression gate (must NOT be deferred)."""
    stripped = cmd.strip()
    return stripped in _PNPM_GATE_COMMANDS


def _is_runtime_only(cmd: str) -> bool:
    """Return True if command matches runtime-only patterns."""
    stripped = cmd.strip()
    return any(p.match(stripped) for p in _RUNTIME_ONLY_PATTERNS)


def _repair_runtime_commands(body: str) -> tuple[str, list[dict]]:
    """Annotate allowlist-outside runtime-only commands with deferred markers.

    Adds the canonical ``# preflight-scope: pr_review_only`` marker (no
    trailing suffix; the shared parser only accepts the exact scope value)
    before the command. The human-readable reason is kept only in the
    independent ``repairs[].reason`` field, never in the marker line.
    Does NOT modify:
    - pnpm typecheck/lint/test/build (regression gates)
    - commands in denylist (curl, rm, bash -c, etc.)
    - commands already annotated with preflight-scope or baseline-expect

    Returns (repaired_body, repairs[])
    """
    repairs: list[dict] = []
    result = body

    def _replace_vc_section(match: re.Match) -> str:
        header = match.group(1)
        section_body = match.group(2)
        repaired, section_repairs = _annotate_runtime_commands(section_body)
        repairs.extend(section_repairs)
        return header + repaired

    result = _VC_SECTION_RE.sub(_replace_vc_section, result)
    return result, repairs


def _annotate_runtime_commands(section: str) -> tuple[str, list[dict]]:
    """Annotate runtime-only commands within a VC section."""
    repairs: list[dict] = []
    lines = section.split("\n")
    new_lines: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        m = _COMMAND_LINE_RE.match(line.strip())
        if m:
            cmd = m.group(1).strip()

            # Check if previous line already has a marker (idempotency)
            prev_line = lines[i - 1].strip() if i > 0 else ""
            already_annotated = (
                _PREFLIGHT_SCOPE_ALREADY_RE.match(prev_line)
                or _BASELINE_EXPECT_ALREADY_RE.match(prev_line)
                or _ALREADY_ANNOTATED_RE.search(line)
            )

            if (
                not already_annotated
                and not _is_pnpm_gate(cmd)
                and not _is_denylist_command(cmd)
                and _is_runtime_only(cmd)
            ):
                # Preserve leading indent of the command line
                indent = len(line) - len(line.lstrip())
                indent_str = line[:indent]
                marker = f"{indent_str}# preflight-scope: pr_review_only"
                new_lines.append(marker)
                repairs.append({
                    "kind": "runtime_only_command",
                    "line_start": i + 1,
                    "line_end": i + 1,
                    "reason": f"command_not_in_allowlist_runtime_only: {cmd}",
                    "original": line.rstrip(),
                    "repaired": marker + "\n" + line.rstrip(),
                })
        new_lines.append(line)
        i += 1

    return "\n".join(new_lines), repairs


# ---------------------------------------------------------------------------
# Main repair pass
# ---------------------------------------------------------------------------


def _scan_unquoted_inline_baseline_expect(line: str) -> tuple[Optional[str], Optional[int]]:
    """Return (annotation_text, start_index) for an UNQUOTED inline
    '# baseline-expect: <pass|fail|deferred>' in the line, else (None, None).
    Quote-aware: occurrences inside single/double quotes are ignored."""
    in_single = False
    in_double = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "#" and not in_single and not in_double:
            m = re.match(r"#\s*baseline-expect:\s*(?:pass|fail|deferred)\b", line[i:])
            if m:
                return line[i:i + m.end()], i
        i += 1
    return None, None


def _repair_inline_baseline_expect(section: str) -> tuple[str, list[dict]]:
    """
    AC3/AC16: move an inline '# baseline-expect:' annotation to the preceding line.
    Only operates on command lines INSIDE ```bash fenced blocks, and only on
    UNQUOTED occurrences (quoted literals are left untouched). Emits structured
    repair records with line_start/line_end/kind/reason/original/repaired/safety/confidence.
    """
    repairs: list[dict] = []
    lines = section.split('\n')
    result_lines: list[str] = []
    in_bash_fence = False
    fence_re = re.compile(r'^\s*```(\w*)\s*$')

    for idx, line in enumerate(lines):
        fence_match = fence_re.match(line)
        if fence_match:
            lang = fence_match.group(1)
            if not in_bash_fence:
                in_bash_fence = (lang == 'bash')
            else:
                in_bash_fence = False
            result_lines.append(line)
            continue

        if in_bash_fence and not line.lstrip().startswith('#'):
            annotation, start = _scan_unquoted_inline_baseline_expect(line)
            if annotation is not None:
                clean_line = line[:start].rstrip()
                result_lines.append(annotation)
                result_lines.append(clean_line)
                repairs.append({
                    "kind": "move_inline_baseline_expect_to_preceding_line",
                    "line_start": idx + 1,
                    "line_end": idx + 1,
                    "reason": (
                        "inline baseline-expect alters command semantics; it must be on the immediately preceding"
                        " comment line"
                    ),
                    "original": line,
                    "repaired": f"{annotation}\n{clean_line}",
                    "safety": "mutation-free-dry-run",
                    "confidence": "high",
                })
                continue

        result_lines.append(line)

    return '\n'.join(result_lines), repairs


def _extract_allowed_paths_ric(body: str) -> list:
    """Parse the '## Allowed Paths' section bullets into a list of path strings."""
    m = re.search(r'^##\s+Allowed Paths\s*$', body, re.MULTILINE)
    if not m:
        return []
    start = m.end()
    nxt = re.search(r'^##\s', body[start:], re.MULTILINE)
    section = body[start:start + nxt.start()] if nxt else body[start:]
    paths = []
    for line in section.split('\n'):
        # Issue #2783: judge no-path marker BEFORE the bullet-token regex
        # below -- a marker line contributes 0 paths.
        if is_no_path_marker(line.strip()):
            continue
        lm = re.match(r'^\s*[-*]\s+`?([^`\s]+)`?\s*$', line)
        if lm:
            paths.append(lm.group(1))
    return paths


def _new_allowed_path_target_ric(command: str, allowed: list, cwd: str):
    """Return a `test -f|-e|-s PATH` / `rg ... PATH` target that is within Allowed
    Paths and does not exist at cwd, else None (mirrors baseline_vc_preflight)."""
    if not allowed:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    norm = [p.strip().lstrip("./").rstrip("/") for p in allowed if p.strip()]

    def _in(p: str) -> bool:
        pp = p.lstrip("./")
        return any(pp == a or pp.startswith(a + "/") for a in norm)

    cands = []
    if prog == "test":
        cands = [a for a in argv[1:] if not a.startswith("-")]
    elif prog == "rg":
        non_opt = [a for a in argv[1:] if not a.startswith("-")]
        cands = non_opt[1:]
    else:
        return None
    base = cwd or "."
    for c in cands:
        if _in(c) and not os.path.exists(os.path.join(base, c)):
            return c
    return None


def _repair_insert_baseline_expect(body: str, cwd: str = ".") -> tuple[str, list[dict]]:
    """AC4/AC8: insert a missing '# baseline-expect:' annotation on the preceding
    line for VC commands inside '## Verification Commands' bash fences:
      - regression-gate commands (pnpm typecheck/lint/test/build) -> baseline-expect: pass
      - commands targeting a NEW Allowed Path (test -f / rg PATH that is in Allowed
        Paths and does not exist at cwd) -> baseline-expect: fail
    Commands that already have a preceding annotation (or an inline one) are skipped.
    Idempotent and dry-run by default (caller decides whether to apply)."""
    repairs: list[dict] = []
    vc_match = re.search(r'^## Verification Commands\s*$', body, re.MULTILINE)
    if not vc_match:
        return body, repairs
    vc_start = vc_match.start()
    next_section = re.search(r'^##\s', body[vc_start + 1:], re.MULTILINE)
    vc_end = vc_start + next_section.start() + 1 if next_section else len(body)
    section = body[vc_start:vc_end]
    allowed = _extract_allowed_paths_ric(body)

    lines = section.split('\n')
    out: list[str] = []
    in_bash = False
    fence_re = re.compile(r'^\s*```(\w*)\s*$')
    for idx, line in enumerate(lines):
        fm = fence_re.match(line)
        if fm:
            if not in_bash:
                in_bash = (fm.group(1) == 'bash')
            else:
                in_bash = False
            out.append(line)
            continue
        if in_bash:
            m = re.match(r'^(\s*)\$\s+(.+)$', line)
            if m:
                indent, cmd = m.group(1), m.group(2).strip()
                if cmd and not cmd.startswith('#'):
                    prev = next((ln for ln in reversed(out) if ln.strip()), "")
                    already = bool(re.match(r'^\s*#\s*baseline-expect:\s*(pass|fail|deferred)\b', prev))
                    has_inline = _scan_unquoted_inline_baseline_expect(line)[0] is not None
                    if not already and not has_inline:
                        ann = None
                        kind = None
                        # Only insert baseline-expect: fail for a NEW Allowed Path target.
                        # Regression-gate baseline-expect: pass insertion is intentionally
                        # NOT auto-applied: it conflicts with the existing Pass-3 runtime
                        # annotation (e.g. `pnpm test:e2e` -> runtime-only) and idempotence
                        # contracts enforced by the existing test suite.
                        tgt = _new_allowed_path_target_ric(cmd, allowed, cwd)
                        if tgt is not None:
                            ann = indent + "# baseline-expect: fail"
                            kind = "insert_baseline_expect_fail"
                        if ann is not None:
                            out.append(ann)
                            repairs.append({
                                "kind": kind,
                                "line_start": idx + 1,
                                "line_end": idx + 1,
                                "reason": "VC is missing a baseline-expect annotation; inserted on the preceding line",
                                "original": line,
                                "repaired": ann + "\n" + line,
                                "safety": "mutation-free-dry-run",
                                "confidence": "high",
                            })
        out.append(line)
    new_section = '\n'.join(out)
    if repairs:
        body = body[:vc_start] + new_section + body[vc_end:]
    return body, repairs


# ---------------------------------------------------------------------------
# Issue #3020: `baseline-expect: deferred` misused as a `pr_review_only`
# stand-in for a pytest VC.
#
# The real producer (`baseline_vc_preflight.py`) classifies such a VC as
# `baseline_expect_deferred` (runner: exec / classification: skipped), which the
# Step 4 authoritative adjudicator (`adjudicate_vc_result.py step4-adjudicate`)
# rejects as `unsupported_baseline_classification`. The canonical form is
# `# preflight-scope: pr_review_only`. This pass rewrites ONLY the annotation
# line, only for a deliberately narrow, deterministic condition set; every
# other `deferred` is left untouched (and is NOT turned into a
# human_review_required record).
# ---------------------------------------------------------------------------

_DEFERRED_ANNOTATION_LINE_RE = re.compile(r"^#\s*baseline-expect:\s*deferred\s*$")
_SHELL_OPERATOR_RE = re.compile(r"&&|\|\||\||;|>|<|`|\$\(")
_PYTEST_VALUE_FLAGS = frozenset({
    "-k", "-m", "-p", "-c", "-o", "--maxfail", "--deselect", "--ignore",
    "--rootdir", "--junitxml", "--durations",
})


# `uv run` option forms that the repo's existing pytest VCs actually use. Any other
# flag makes the launcher ambiguous and the VC is left untouched.
_UV_RUN_FLAGS_NO_VALUE = frozenset({
    "--locked", "--frozen", "--no-project", "--offline", "--isolated", "--no-sync",
})
_UV_RUN_FLAGS_WITH_VALUE = frozenset({"--with"})
_PYTHON_LAUNCHERS = frozenset({"python", "python3"})


def _pytest_launcher_args(tokens: list[str]) -> Optional[list[str]]:
    """Return the argv after a recognised pytest launcher PREFIX, else None.

    Recognised forms (the ones existing VCs use), anchored at tokens[0]:
      pytest ...
      python|python3 -m pytest ...
      uv run [--locked|--frozen|--no-project|--offline|--isolated|--no-sync|--with <pkg>|--with=<pkg>]...
          (pytest | python|python3 -m pytest) ...
    A ``pytest`` token anywhere else (e.g. an ``rg`` pattern) is NOT a launcher.
    """
    i = 0
    if tokens[:2] == ["uv", "run"]:
        i = 2
        while i < len(tokens) and tokens[i].startswith("-"):
            tok = tokens[i]
            if tok in _UV_RUN_FLAGS_NO_VALUE or (tok.startswith("--with=") and len(tok) > len("--with=")):
                i += 1
            elif tok in _UV_RUN_FLAGS_WITH_VALUE and i + 1 < len(tokens):
                i += 2
            else:
                return None
    if i >= len(tokens):
        return None
    if tokens[i] == "pytest":
        return tokens[i + 1:]
    if tokens[i] in _PYTHON_LAUNCHERS and tokens[i + 1:i + 3] == ["-m", "pytest"]:
        return tokens[i + 3:]
    return None


def _pytest_test_file_targets(cmd: str) -> Optional[list[str]]:
    """Return the repo-relative test file paths a single pytest VC targets, or
    None when the command is not a simple single pytest invocation (recognised
    launcher prefix only) over explicit ``*.py`` test files (directories,
    globs, compound commands, ambiguous launchers, unparsable shell all yield
    None)."""
    if _SHELL_OPERATOR_RE.search(cmd):
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    rest = _pytest_launcher_args(tokens)
    if rest is None:
        return None
    targets: list[str] = []
    skip_next = False
    for tok in rest:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in _PYTEST_VALUE_FLAGS:
                skip_next = True
            continue
        path = tok.split("::", 1)[0]
        if not path.endswith(".py") or any(ch in path for ch in "*?[]"):
            return None
        targets.append(path)
    return targets or None


def _is_test_file_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _path_in_allowed(path: str, allowed: list) -> bool:
    norm = path[2:] if path.startswith("./") else path
    if norm.startswith("/") or ".." in norm.split("/"):
        return False
    for raw in allowed:
        a = raw[2:] if raw.startswith("./") else raw
        if norm == a or (a.endswith("/") and norm.startswith(a)):
            return True
    return False


def _repair_deferred_to_pr_review_only(body: str) -> tuple[str, list[dict]]:
    new_body, repairs, _cmd_lines = _repair_deferred_to_pr_review_only_ex(body)
    return new_body, repairs


def _repair_deferred_to_pr_review_only_ex(body: str) -> tuple[str, list[dict], list[str]]:
    """Replace the annotation line of a misused ``# baseline-expect: deferred``
    pytest VC with ``# preflight-scope: pr_review_only`` (Issue #3020).

    Applies only when ALL hold (otherwise the VC is left byte-identical):
      - the command is a single pytest invocation whose positional targets are
        explicit ``*.py`` test files, each inside the Issue's Allowed Paths;
      - the real producer would classify it ``baseline_expect_deferred``: no
        preflight-scope marker on the immediately preceding line, no unquoted
        inline baseline-expect in the command, and the shared annotation
        grammar resolves ``baseline-expect`` to ``deferred``;
      - the ``deferred`` annotation is the single baseline-expect line and sits
        immediately before the command (so the replacement marker is adjacent,
        the placement the producer requires for ``preflight-scope``);
      - the command is not a denylisted / pnpm regression-gate command.

    The command line is never modified, so the command hash is unchanged;
    only the annotation line (and therefore the body hash) changes. Idempotent:
    after the rewrite the VC has a valid scope marker and no longer matches.
    """
    repairs: list[dict] = []
    cmd_lines: list[str] = []  # command line (verbatim) paired 1:1 with repairs
    if _extract_baseline_expect_annotation is None or _parse_preflight_scope_marker_line is None:
        return body, repairs, cmd_lines
    vc_match = re.search(r'^## Verification Commands\s*$', body, re.MULTILINE)
    if not vc_match:
        return body, repairs, cmd_lines
    vc_start = vc_match.start()
    next_section = re.search(r'^##\s', body[vc_start + 1:], re.MULTILINE)
    vc_end = vc_start + next_section.start() + 1 if next_section else len(body)
    section = body[vc_start:vc_end]
    allowed = _extract_allowed_paths_ric(body)
    if not allowed:
        return body, repairs, cmd_lines

    lines = section.split('\n')
    fence_re = re.compile(r'^\s*```(\w*)\s*$')
    in_bash = False
    block_start = 0  # index in `lines` of the first line inside the current bash fence
    for idx, line in enumerate(lines):
        fm = fence_re.match(line)
        if fm:
            if not in_bash:
                in_bash = (fm.group(1) == 'bash')
                block_start = idx + 1
            else:
                in_bash = False
            continue
        if not in_bash:
            continue
        m = re.match(r'^(\s*)\$\s+(.+)$', line)
        if not m:
            continue
        cmd = m.group(2).strip()
        block_idx = idx - block_start
        if block_idx < 1 or not cmd or cmd.startswith('#'):
            continue
        if _is_pnpm_gate(cmd) or _is_denylist_command(cmd):
            continue
        # Mirror the real producer's baseline_expect_deferred classification.
        block_lines = [ln.strip() for ln in lines[block_start:idx + 1]]
        scope_marker, _ = _parse_preflight_scope_marker_line(block_lines[block_idx - 1])
        if scope_marker is not None:
            continue
        if _scan_unquoted_inline_baseline_expect(line)[0] is not None:
            continue
        value, ann_line_no, ann_raw = _extract_baseline_expect_annotation(block_lines, block_idx)
        if value != "deferred" or ann_raw is None:
            continue
        # Annotation must be the only baseline-expect line and immediately precede the command.
        if ann_line_no != block_idx or not _DEFERRED_ANNOTATION_LINE_RE.match(ann_raw.strip()):
            continue
        targets = _pytest_test_file_targets(cmd)
        if not targets:
            continue
        if not all(_is_test_file_path(t) and _path_in_allowed(t, allowed) for t in targets):
            continue

        ann_idx = idx - 1
        original_ann = lines[ann_idx]
        ann_indent = original_ann[:len(original_ann) - len(original_ann.lstrip())]
        new_ann = f"{ann_indent}# preflight-scope: pr_review_only"
        lines[ann_idx] = new_ann
        repairs.append({
            "kind": "deferred_to_pr_review_only",
            "line_start": ann_idx + 1,
            "line_end": ann_idx + 1,
            "reason": (
                "baseline-expect: deferred is classified baseline_expect_deferred by the producer and "
                "rejected by Step 4 (unsupported_baseline_classification); a post-implementation "
                "pytest VC must use the canonical preflight-scope: pr_review_only"
            ),
            "original": original_ann,
            "repaired": new_ann,
            "safety": "mutation-free-dry-run",
            "confidence": "high",
        })
        cmd_lines.append(lines[idx])
    if not repairs:
        return body, repairs, cmd_lines
    return body[:vc_start] + '\n'.join(lines) + body[vc_end:], repairs, cmd_lines


def _collapse_inline_deferred_chain(
    all_repairs: list[dict],
    inline_repairs: list[dict],
    deferred_repairs: list[dict],
    deferred_cmd_lines: list[str],
) -> list[dict]:
    """Collapse the dependent Pass 2 -> Pass 2.6 chain into ONE repair record.

    For ``$ pytest X  # baseline-expect: deferred`` Pass 2 moves the inline
    annotation to its own preceding line and Pass 2.6 then rewrites that very
    line. Reported as two records they overlap (``overlapping_repair``) although
    they are one original->final repair. Each deferred record whose annotation
    line + command line equal the ``repaired`` text of an inline-move record
    (matched by content, in order; line numbers of later passes are not
    comparable once earlier passes inserted lines) is merged with it into a
    single ``deferred_to_pr_review_only`` record whose ``original`` is the
    inline source line and whose ``repaired`` is the final two lines.
    ``classify_repair_action`` overlap detection is untouched: independent
    overlapping records are still rejected.

    Mutates ``all_repairs`` (drops the consumed inline records) and returns
    the deferred-pass records to append (merged where chained).
    """
    consumed: set[int] = set()
    merged: list[dict] = []
    for rec, cmd_line in zip(deferred_repairs, deferred_cmd_lines):
        wanted = f"{rec['original']}\n{cmd_line}"
        match = next(
            (
                i for i, inl in enumerate(inline_repairs)
                if i not in consumed and inl.get("repaired") == wanted
            ),
            None,
        )
        if match is None:
            merged.append(rec)
            continue
        consumed.add(match)
        inl = inline_repairs[match]
        merged.append({
            "kind": rec["kind"],
            "line_start": inl["line_start"],
            "line_end": inl["line_end"],
            "reason": (
                f"{rec['reason']} (chained after {inl['kind']}: the inline annotation was first "
                "moved to its own preceding line, then rewritten; reported as one repair)"
            ),
            "original": inl["original"],
            "repaired": f"{rec['repaired']}\n{cmd_line}",
            "safety": rec["safety"],
            "confidence": rec["confidence"],
        })
    if consumed:
        drop = {id(inline_repairs[i]) for i in consumed}
        all_repairs[:] = [r for r in all_repairs if id(r) not in drop]
    return merged


def repair_body(body: str) -> tuple[str, list[dict]]:
    """Run all repair passes in order.  Returns (repaired_body, all_repairs[])."""
    all_repairs: list[dict] = []
    repairs_inline: list[dict] = []

    # Pass 1: escaped code fence repair (Machine-Readable Contract section only)
    body, repairs1 = _repair_escaped_code_fences(body)
    all_repairs.extend(repairs1)

    # Pass 2: inline baseline-expect annotation repair (Verification Commands section only)
    # Extract Verification Commands section for targeted repair
    vc_match = re.search(r'^## Verification Commands\s*$', body, re.MULTILINE)
    if vc_match:
        vc_start = vc_match.start()
        # Find next ## section
        next_section = re.search(r'^##\s', body[vc_start + 1:], re.MULTILINE)
        if next_section:
            vc_end = vc_start + next_section.start() + 1
        else:
            vc_end = len(body)
        
        vc_section = body[vc_start:vc_end]
        vc_repaired, repairs_inline = _repair_inline_baseline_expect(vc_section)
        if repairs_inline:
            body = body[:vc_start] + vc_repaired + body[vc_end:]
            all_repairs.extend(repairs_inline)

    # Pass 2.5: insert missing baseline-expect annotations (Issue #899)
    body, repairs_insert = _repair_insert_baseline_expect(body)
    all_repairs.extend(repairs_insert)

    # Pass 2.6: misused `baseline-expect: deferred` pytest VC -> canonical
    # `preflight-scope: pr_review_only` (Issue #3020; annotation line only)
    body, repairs_deferred, deferred_cmd_lines = _repair_deferred_to_pr_review_only_ex(body)
    # (a chain Pass 2 -> Pass 2.6 on one VC is reported as ONE record, not two overlapping ones)
    repairs_deferred = _collapse_inline_deferred_chain(
        all_repairs, repairs_inline, repairs_deferred, deferred_cmd_lines
    )
    all_repairs.extend(repairs_deferred)

    # Pass 3: runtime-only command annotation (Verification Commands section only)
    body, repairs3 = _repair_runtime_commands(body)
    all_repairs.extend(repairs3)

    return body, all_repairs



# ---------------------------------------------------------------------------
# Candidate materialization security (Issue #2016 iteration-3 OWNER
# adversarial review P0-2): the candidate body path is a fixed, predictable
# location. A plain Path.write_text() there follows a pre-existing symlink
# and overwrites whatever it points at. These helpers fail closed on:
#   - a pre-existing symlink / FIFO / device / directory at the leaf path
#   - a symlinked ancestor directory between the leaf and the caller-supplied
#     artifact root
# and only ever materialize the candidate via a securely-created sibling
# temp file (O_CREAT|O_EXCL|O_NOFOLLOW) that is fsync'd, hash-verified, and
# then atomically os.replace()'d onto the final leaf path.
# ---------------------------------------------------------------------------


class CandidateWriteSecurityError(Exception):
    """Raised when the candidate body materialization path fails a
    fail-closed leaf/ancestor safety check."""


def _reject_unsafe_leaf(path: Path) -> None:
    """Fail-closed leaf check: reject a pre-existing symlink / FIFO /
    device / directory at `path`. Uses os.lstat (does NOT follow symlinks)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CandidateWriteSecurityError(f"candidate_leaf_lstat_error:{path}:{exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise CandidateWriteSecurityError(f"candidate_leaf_is_symlink:{path}")
    if stat.S_ISDIR(st.st_mode):
        raise CandidateWriteSecurityError(f"candidate_leaf_is_directory:{path}")
    if not stat.S_ISREG(st.st_mode):
        raise CandidateWriteSecurityError(f"candidate_leaf_not_regular_file:{path}")


def _reject_unsafe_parent_chain(path: Path, root: Optional[Path]) -> None:
    """Reject if any ancestor directory of `path` (up to and including
    `root`, when given) is itself a symlink, and verify the resolved parent
    is contained within `root`. Fail-closed: an unresolvable ancestor or an
    ancestor outside `root` is rejected."""
    node = path.parent
    while True:
        try:
            st = os.lstat(node)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(st.st_mode):
            raise CandidateWriteSecurityError(f"candidate_ancestor_is_symlink:{node}")
        if root is not None and node.resolve(strict=False) == root.resolve(strict=False):
            break
        parent = node.parent
        if parent == node:
            break
        node = parent

    if root is not None:
        try:
            resolved_parent = path.parent.resolve(strict=False)
            resolved_root = root.resolve(strict=False)
        except Exception as exc:
            raise CandidateWriteSecurityError(f"candidate_parent_unresolvable:{exc}") from exc
        if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
            raise CandidateWriteSecurityError(
                f"candidate_parent_outside_root:{resolved_parent}:not_under:{resolved_root}"
            )


def secure_atomic_write_candidate(path: Path, text: str, *, root: Optional[Path] = None) -> None:
    """Write `text` to `path` with fail-closed symlink/parent-symlink
    protection (Issue #2016 iteration-3 P0-2). Never truncates through a
    pre-existing symlink: creates a securely-named sibling temp file with
    O_CREAT|O_EXCL|O_NOFOLLOW, fsyncs it, re-checks the leaf immediately
    before replace (narrowing the TOCTOU window), then atomically
    os.replace()s it onto the final leaf path."""
    _reject_unsafe_parent_chain(path, root)
    _reject_unsafe_leaf(path)

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    encoded = text.encode("utf-8")

    tmp_name = f".{path.name}.{os.getpid()}.{int(time.time() * 1000000)}.tmp"
    tmp_path = directory / tmp_name
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(str(tmp_path), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-check the leaf immediately before replace: narrows (does not
        # eliminate) the TOCTOU window between the initial check and the
        # rename. os.replace() itself is an atomic rename, so once this
        # check passes the actual swap cannot be intercepted.
        _reject_unsafe_leaf(path)
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise


def run_repair(
    body: str,
    *,
    apply: bool = False,
    out_file: Optional[str] = None,
    root_dir: Optional[str] = None,
) -> dict:
    """Run repair and return the result JSON dict.

    Args:
        body:     Issue body text.
        apply:    If True, write repaired body to out_file (or raise if not given).
        out_file: Path to write repaired body when apply=True.
        root_dir: Optional allowed artifact root for containment verification
                  (Issue #2016 iteration-3 P0-2). When given, out_file's
                  parent directory chain must resolve within this root.
    """
    original_sha = _sha256(body)

    repaired_body, repairs = repair_body(body)

    repaired_sha = _sha256(repaired_body)
    changed = original_sha != repaired_sha

    repair_action = classify_repair_action(original_sha, repaired_sha, repairs)

    if apply:
        if not out_file:
            raise ValueError("--out-file is required when --apply is set")
        written_path = Path(out_file)
        artifact_root = Path(root_dir) if root_dir else None
        secure_atomic_write_candidate(written_path, repaired_body, root=artifact_root)
        # Readback validation: confirm bytes round-trip before advertising
        # the artifact path as a canonical candidate_body_artifact.
        if written_path.read_text(encoding="utf-8") == repaired_body:
            repair_action["candidate_body_artifact"] = str(written_path.resolve())

    return {
        "schema": SCHEMA,
        "dry_run": not apply,
        "changed": changed,
        "original_body_sha256": original_sha,
        "repaired_body_sha256": repaired_sha,
        "repairs": repairs,
        "repair_action": repair_action,
    }


# ---------------------------------------------------------------------------
# Template-derived structural repair (Issue #995)
#
# Detector / classifier / proposal producer for template-required section and
# Machine-Readable Contract key gaps. This is a distinct concern from the
# body-defect repairs above (escaped fences / runtime-only commands /
# baseline-expect annotations): it reads the *resolved GitHub issue-form
# template* (field id, label, required, default value, field order, template
# path, immutable template-file digest) and the *issue_kind authoring SSOT*
# (required Machine-Readable Contract keys; see
# .claude/skills/create-issue/references/body-authoring.md) to find gaps that
# heading-set-membership alone would miss (duplicate / empty / placeholder-only
# / ambiguous-cardinality sections).
#
# Mutation-free: never calls GitHub, never writes the Issue body, never
# dispatches items individually. A single producer run returns one versioned
# handoff bundle listing every missing/ambiguous item in template field
# order, then field id. #2039 (out of scope for #995) owns GitHub mutation,
# authoritative readback, and per-item consumer classification/transaction.
# ---------------------------------------------------------------------------

STRUCTURAL_REPAIR_SCHEMA_VERSION = "structural_repair_action/v1"
STRUCTURAL_REPAIR_POLICY_VERSION = "template-derived-structural-repair/v1"

# Closed derivation-mode enum (Issue #995 Outcome). No other derivation mode
# may ever appear on an auto_apply_safe item.
DERIVATION_TEMPLATE_VALUE_EXACT = "template_value_exact"
DERIVATION_SOURCE_SPAN_EXACT = "source_span_exact"
DERIVATION_DERIVED_SCALAR_EXACT = "derived_scalar_exact"
CLOSED_DERIVATION_MODES = frozenset({
    DERIVATION_TEMPLATE_VALUE_EXACT,
    DERIVATION_SOURCE_SPAN_EXACT,
    DERIVATION_DERIVED_SCALAR_EXACT,
})

STRUCT_DISPOSITION_AUTO_APPLY_SAFE = "auto_apply_safe"
STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED = "human_review_required"

# issue_kind -> required Machine-Readable Contract YAML keys.
#
# Issue #995 fix_delta (OWNER REQUEST_CHANGES P1-3): this is no longer a
# hand-transcribed literal copy of the authoring SSOT. It is parsed at
# import time from the actual SSOT document
# (.claude/skills/create-issue/references/body-authoring.md, "Machine-
# Readable Contract Block Guidance" bullet list), so drift between the SSOT
# prose and this policy is caught by test_ssot_policy_digest_changes_when_
# authoring_policy_changes instead of silently accumulating. A hard-coded
# fallback is retained ONLY for the case where the SSOT file is missing or
# its bullet format changes incompatibly (fail-safe bootstrap, not a
# silently-preferred source of truth).
_REQUIRED_CONTRACT_KEYS_BY_KIND_FALLBACK = {
    "parent": [
        "contract_schema_version", "issue_kind", "goal_ref", "change_kind",
        "parent_mode", "closure_mode",
    ],
    "implementation": [
        "contract_schema_version", "issue_kind", "parent_issue", "goal_ref",
        "change_kind",
    ],
    "research": [
        "contract_schema_version", "issue_kind", "parent_issue", "goal_ref",
        "change_kind",
    ],
}

_BODY_AUTHORING_SSOT_PATH = (
    Path(__file__).resolve().parents[4]
    / ".claude" / "skills" / "create-issue" / "references" / "body-authoring.md"
)

# Matches lines of the form:
#   - `parent`: `contract_schema_version`, `issue_kind`, `goal_ref`, `change_kind`, `parent_mode`, `closure_mode`
#   - `implementation` / `research`: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`
_SSOT_REQUIRED_KEYS_LINE_RE = re.compile(
    r"^-\s*(?P<kinds>(?:`[\w-]+`(?:\s*/\s*)?)+):\s*(?P<keys>(?:`[\w-]+`(?:,\s*)?)+)\s*$"
)


def _parse_required_contract_keys_ssot(text: str) -> dict[str, list[str]]:
    """Parse the body-authoring.md "required key ごとの required key を維持する"
    bullet list into {issue_kind: [required_keys]} (Issue #995 P1-3)."""
    result: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        m = _SSOT_REQUIRED_KEYS_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        kinds = [k.strip("` ") for k in m.group("kinds").split("/")]
        keys = [k.strip("` ") for k in m.group("keys").split(",")]
        kinds = [k for k in kinds if k]
        keys = [k for k in keys if k]
        if not kinds or not keys:
            continue
        for kind in kinds:
            result[kind] = keys
    return result


def _load_required_contract_keys_by_kind() -> dict[str, list[str]]:
    try:
        text = _BODY_AUTHORING_SSOT_PATH.read_text(encoding="utf-8")
    except OSError:
        return dict(_REQUIRED_CONTRACT_KEYS_BY_KIND_FALLBACK)
    parsed = _parse_required_contract_keys_ssot(text)
    if not parsed:
        return dict(_REQUIRED_CONTRACT_KEYS_BY_KIND_FALLBACK)
    return parsed


REQUIRED_CONTRACT_KEYS_BY_KIND = _load_required_contract_keys_by_kind()

# The canonical closed issue_kind enum is derived from the same SSOT-parsed
# policy (its keys), not re-declared as an independent literal (Issue #995
# P0-5 test_unknown_issue_kind_fails_closed).
_CANONICAL_ISSUE_KINDS = frozenset(REQUIRED_CONTRACT_KEYS_BY_KIND.keys())

# Template field ids whose `attributes.value` is a real, byte-exact, non-
# placeholder default (boilerplate the template author already committed to)
# rather than a `placeholder:`-only authoring hint. Only these field ids are
# ever eligible for template_value_exact auto_apply_safe classification
# (AC4). All other required fields are semantic/free-form and MUST be
# human_review_required when missing (AC3) even though they are `required`
# in the template.
_TEMPLATE_VALUE_AUTO_SAFE_FIELD_IDS = frozenset({
    "machine-readable-contract",
    "runtime-verification-applicability",
    "verification-commands",
    "stop-conditions",
    "required-skills",
    "scope-delta",
})

# A `<required: ...>` placeholder token left un-replaced in a resolved field.
_REQUIRED_PLACEHOLDER_RE = re.compile(r"^<required:[^>]*>$")

# Issue #995 fix_delta (P1-1): CommonMark-compatible-enough ATX H2 heading
# match (up to 3 leading spaces, optional trailing closing-hash run) --
# reused verbatim from mrc_contract_parser.py's already-tested `_H2_RE` so
# heading recognition does not diverge across the two producers.
_H2_HEADING_RE = re.compile(r"^[ ]{0,3}##[ \t]+(?P<heading>.+?)[ \t]*#*[ \t]*$")
# Fence OPEN: up to 3 leading spaces, 3+ backtick/tilde chars, optional trailing
# info string (anything). Fence CLOSE: up to 3 leading spaces, 3+ same-char
# fence chars, and NOTHING else (CommonMark: a closing fence's marker line may
# only be followed by spaces/tabs). Issue #995 P1-1 fix: the previous
# `_FENCE_LINE_RE` matched a leading fence marker regardless of trailing text,
# so ` ```not-a-close ` was treated as a real closing fence, letting a
# fenced-code example containing a fake `## Heading` re-open scope outside
# the code block.
_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_ONLY_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*$")

# Issue #995 fix_delta (P0-4): a template's `attributes.value` default can be
# a placeholder/scaffold string (an angle-bracket hint like
# `<parent-issue-number>`, or an unselected `a|b|c` enum) rather than a real
# committed value. Both must be rejected from `template_value_exact` /
# `auto_apply_safe` -- `validations.required` and a non-empty `value` alone
# do NOT prove semantic completeness (GitHub Issue Forms `value` is just a
# textarea pre-fill, not a validity claim).
_PLACEHOLDER_ANGLE_TOKEN_RE = re.compile(r"<[^<>\n]{1,160}>")
_UNSELECTED_ENUM_VALUE_RE = re.compile(
    r"^[\w][\w\-./ ]*(\|[\w][\w\-./ ]*){1,}$"
)


def _contains_unselected_enum_scaffold(text: str) -> bool:
    """True when any line of `text` carries a bare unselected `a|b|c` enum
    scaffold (Issue #995 P0-4)."""
    if not isinstance(text, str):
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("```") or line.startswith("~~~"):
            continue
        rhs = line.split(":", 1)[1].strip() if ":" in line else line
        rhs = rhs.strip().strip('"').strip("'").strip()
        if "|" in rhs and _UNSELECTED_ENUM_VALUE_RE.match(rhs):
            return True
    return False


def _contains_placeholder_scaffold(text: str) -> bool:
    """Recursively (line-by-line) reject `<...>` authoring hints and bare
    unselected `a|b|c` enum scaffolds anywhere in `text` (Issue #995 P0-4)."""
    if not isinstance(text, str):
        return False
    if _PLACEHOLDER_ANGLE_TOKEN_RE.search(text):
        return True
    return _contains_unselected_enum_scaffold(text)


# Issue #3015: the only Machine-Readable Contract key whose value is free
# prose. Every other MRC key has a closed value format and keeps the strict
# `_contains_placeholder_scaffold` detection (fail-closed default).
_MRC_FREE_TEXT_KEYS = frozenset({"goal_ref"})


def _extract_template_mrc_default(block_text: object, key: str) -> Optional[str]:
    """Return the raw (placeholder-agnostic) string default of `key` in a
    template field's fenced-YAML `value`, or None when unresolvable."""
    if not isinstance(block_text, str):
        return None
    m = re.search(r"```ya?ml\n(.*?)```", block_text, re.DOTALL)
    if not m:
        return None
    import yaml as _yaml
    try:
        data = _yaml.safe_load(m.group(1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if not isinstance(value, str):
        return None
    return value


def _is_placeholder_mrc_scalar(key: str, value: str, template_default: Optional[str]) -> bool:
    """Issue #3015: placeholder judgement for an already-parsed MRC scalar.

    Closed-format keys (everything but `goal_ref`) and an unresolvable
    template default keep the current strict `_contains_placeholder_scaffold`
    behaviour (fail-closed). For the free-text key `goal_ref`, a value is a
    placeholder only when (1) the whole value is a single angle token, (2) it
    equals (strip, exact) the same key's placeholder default in the template,
    or (3) it is an unselected `a|b|c` enum. A value that merely contains a
    partial angle description (e.g. `sha256:<hex>` in prose) is legitimate.
    """
    if key not in _MRC_FREE_TEXT_KEYS or not isinstance(template_default, str):
        return _contains_placeholder_scaffold(value)
    stripped = value.strip()
    token = _PLACEHOLDER_ANGLE_TOKEN_RE.fullmatch(stripped)
    if token is not None:
        return True
    default = template_default.strip()
    if stripped == default and _contains_placeholder_scaffold(default):
        return True
    return _contains_unselected_enum_scaffold(stripped)


def _extract_template_declared_scalar(block_text: object, key: str) -> Optional[str]:
    """Extract a single top-level scalar key's value from a template field's
    fenced-YAML `value` default (e.g. the real `contract_schema_version: v1`
    already committed inside the implementation template's Machine-Readable
    Contract scaffold), instead of hard-coding that value independently
    (Issue #995 P0-4)."""
    value = _extract_template_mrc_default(block_text, key)
    if value is None or not value.strip() or _contains_placeholder_scaffold(value):
        return None
    return value.strip()


# Issue #995 fix_delta (P0-3): `derived_scalar_exact` field-specific closed
# validators. A field WITHOUT an entry here still uses the generic syntactic
# guard (`_is_syntactic_scalar`) as a weaker fallback -- but any field listed
# here MUST match its own closed pattern, closing the
# `known_scalars={"parent-issue": "foobar"}` hole the OWNER review flagged.
_DERIVED_SCALAR_FIELD_VALIDATORS: dict[str, re.Pattern] = {
    "parent-issue": re.compile(r"^(?:none|#[1-9][0-9]*)$"),
    "parent_issue": re.compile(r"^(?:none|#[1-9][0-9]*)$"),
    "machine-readable-contract.parent_issue": re.compile(r"^(?:none|#[1-9][0-9]*)$"),
}

# Issue #995 fix_delta (P0-3): `source_span_exact` provenance fields that MUST
# all be present (non-null, non-empty) before a source span can ever back an
# `auto_apply_safe` item -- authority alone (a non-empty `text`) is not
# sufficient. `text` is the raw source bytes (used to compute
# `source_text_sha256` and cross-check `candidate_sha256`), not part of the
# emitted provenance object itself.
# Issue #2431: `current_issue` is an additive extension -- a same-body,
# closed-heading-alias-only authority kind used ONLY when the source span's
# text lives in the SAME Issue body being repaired (never a different Issue
# or comment). The caller (`run_refinement_preflight.py`'s
# `_STRUCTURAL_HEADING_ALIAS_TABLE` assembler) is solely responsible for only
# ever emitting a `current_issue` span when every one of the narrow
# preconditions in that assembler's docstring hold; this module's own
# provenance validation (`_validate_source_span_provenance`) applies the SAME
# generic required-field/enum checks to it as to the other 3 kinds.
_SOURCE_SPAN_AUTHORITY_KINDS = frozenset(
    {"parent_issue", "owner_anchor", "design_reference", "current_issue"}
)
_SOURCE_SPAN_OBJECT_KINDS = frozenset({"issue_body", "issue_comment", "git_blob"})
_SOURCE_SPAN_REQUIRED_FIELDS = (
    "authority_kind", "source_repo", "source_object_kind", "source_object_id",
    "source_url", "source_revision", "line_start", "line_end", "text",
)


def _validate_source_span_provenance(span: dict) -> tuple[bool, list[str]]:
    """Return (ok, reason_codes). Fail-closed: any missing/empty required
    provenance field, or an out-of-enum authority_kind/source_object_kind, or
    a malformed line range, rejects the span from `auto_apply_safe`."""
    reasons: list[str] = []
    for field_name in _SOURCE_SPAN_REQUIRED_FIELDS:
        value = span.get(field_name)
        if value is None:
            reasons.append(f"source_span_missing_{field_name}")
        elif isinstance(value, str) and value.strip() == "":
            reasons.append(f"source_span_missing_{field_name}")
    if reasons:
        return False, reasons
    if span.get("authority_kind") not in _SOURCE_SPAN_AUTHORITY_KINDS:
        reasons.append("source_span_invalid_authority_kind")
    if span.get("source_object_kind") not in _SOURCE_SPAN_OBJECT_KINDS:
        reasons.append("source_span_invalid_object_kind")
    line_start, line_end = span.get("line_start"), span.get("line_end")
    valid_range = (
        isinstance(line_start, int) and isinstance(line_end, int)
        and not isinstance(line_start, bool) and not isinstance(line_end, bool)
        and line_start >= 1 and line_end >= line_start
    )
    if not valid_range:
        reasons.append("source_span_invalid_line_range")
    return (len(reasons) == 0), reasons


def _validate_current_issue_source_span_authority(
    span: dict,
    *,
    repo: Optional[str],
    issue_number: Optional[int],
    original_body_sha256: str,
) -> bool:
    """`original_body_sha256` is the ALREADY `sha256:`-prefixed digest this
    module's own `_sha256()` produces (the SAME convention
    `build_structural_repair_bundle()`'s `original_body_sha256 = _sha256(body)`
    and the bundle's own top-level `original_body_sha256` field already use)
    -- compared directly against `span["source_revision"]`, never
    re-prefixed a second time.

    PR #2469 fix_delta iteration 3 (P1-2, human adversarial review):
    `_validate_source_span_provenance()` above only enforces the GENERIC
    required-field/enum/line-range shape shared by all 4
    `_SOURCE_SPAN_AUTHORITY_KINDS` -- it has no way to know this run's own
    `repo`/`issue_number`/`original_body_sha256`, so it cannot tell a
    genuine same-body `current_issue` span from a FORGED one (e.g. a
    `source_object_kind: issue_comment` or a different `source_repo`/
    `source_object_id`/stale `source_revision`) claiming the
    `current_issue` authority_kind. `current_issue`'s own narrower
    precondition -- same repo, same issue, body digest matches THIS run's
    `original_body_sha256` -- can only be checked at a boundary that
    actually has those 3 values, so it lives HERE at this module's own
    generic producer boundary (`build_structural_repair_bundle()`, the
    caller of this function), never in
    `run_refinement_preflight.py`'s `_STRUCTURAL_HEADING_ALIAS_TABLE`
    assembler -- so ANY caller of `build_structural_repair_bundle()`
    (not just that one trusted assembler) gets this check, never only the
    single production caller. Any span whose `authority_kind` is NOT
    `current_issue` is untouched (returns True unconditionally) -- this is
    an ADDITIVE narrowing for `current_issue` only, never a change to the
    other 3 authority kinds' existing behavior."""
    if span.get("authority_kind") != "current_issue":
        return True
    return (
        span.get("source_object_kind") == "issue_body"
        and repo is not None
        and span.get("source_repo") == repo
        and issue_number is not None
        and span.get("source_object_id") == str(issue_number)
        and span.get("source_revision") == original_body_sha256
    )


def _reject_forged_current_issue_source_spans(
    source_spans: Optional[dict],
    *,
    repo: Optional[str],
    issue_number: Optional[int],
    original_body_sha256: str,
) -> Optional[dict]:
    """PR #2469 fix_delta iteration 3 (P1-2): drop any `source_spans` entry
    (or, for a multi-candidate list entry, any individual candidate) whose
    `authority_kind` is `current_issue` but fails
    `_validate_current_issue_source_span_authority()` -- BEFORE
    `detect_missing_template_sections()` ever sees it, so a rejected span
    can never reach `_classify_missing_field()`'s `text`-based
    `source_span_exact`/`auto_apply_safe` branch. A field with its only
    candidate dropped this way falls through to
    `_classify_missing_field()`'s existing `missing_required_field_
    without_exact_source` -> `human_review_required` path -- fail-closed,
    the SAME disposition an absent source span already produces (never a
    NEW disposition/derivation mode)."""
    if not source_spans:
        return source_spans
    filtered: dict = {}
    for field_id, entry in source_spans.items():
        if isinstance(entry, list):
            kept = [
                candidate
                for candidate in entry
                if not isinstance(candidate, dict)
                or _validate_current_issue_source_span_authority(
                    candidate, repo=repo, issue_number=issue_number,
                    original_body_sha256=original_body_sha256,
                )
            ]
            if kept:
                filtered[field_id] = kept
            continue
        if isinstance(entry, dict) and not _validate_current_issue_source_span_authority(
            entry, repo=repo, issue_number=issue_number, original_body_sha256=original_body_sha256,
        ):
            continue
        filtered[field_id] = entry
    return filtered


def _validate_owner_anchor_source_span_authority(
    span: dict,
    *,
    field_id: str,
    repo: Optional[str],
    issue_number: Optional[int],
    owner_anchor_snapshot: Optional[dict],
    field_labels: dict[str, str],
) -> bool:
    """Validate an existing ``owner_anchor`` span against the producer's
    separately supplied, trusted anchor snapshot before classification.

    The span itself is untrusted handoff data. Its source URL/id/revision and
    line range must bind to the exact OWNER comment snapshot that preflight
    already structurally resolved; otherwise this generic producer boundary
    drops it before ``source_span_exact`` can become auto-apply-safe.
    """
    if span.get("authority_kind") != "owner_anchor":
        return True
    if not isinstance(owner_anchor_snapshot, dict) or repo is None or issue_number is None:
        return False
    body = owner_anchor_snapshot.get("body")
    comment_id = owner_anchor_snapshot.get("comment_id")
    comment_url = owner_anchor_snapshot.get("comment_url")
    if (
        not isinstance(body, str)
        or not body
        or not isinstance(comment_id, str)
        or not comment_id.isdecimal()
        or not isinstance(comment_url, str)
        or owner_anchor_snapshot.get("repo") != repo
        or owner_anchor_snapshot.get("target_issue_number") != issue_number
        or owner_anchor_snapshot.get("author_association") != "OWNER"
        or not isinstance(owner_anchor_snapshot.get("comment_updated_at"), str)
        or not owner_anchor_snapshot["comment_updated_at"].strip()
        or owner_anchor_snapshot.get("body_sha256") != _sha256(body)
    ):
        return False
    expected_url = f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{comment_id}"
    if (
        comment_url != expected_url
        or span.get("source_url") != expected_url
        or span.get("source_repo") != repo
        or span.get("source_object_kind") != "issue_comment"
        or span.get("source_object_id") != comment_id
        or span.get("source_revision") != _sha256(body)
    ):
        return False
    expected_heading = field_labels.get(field_id)
    if not isinstance(expected_heading, str) or not expected_heading:
        return False
    sections = _parse_h2_sections(body)
    matches = [section for section in sections if section["heading"].strip() == expected_heading]
    if len(matches) != 1:
        return False
    section = matches[0]
    body_lines = body.split("\n")
    # owner_anchor is narrower than the generic CommonMark-compatible parser:
    # only its literal, column-0 canonical H2 can back source_span_exact.
    if body_lines[section["start_line"] - 1] != f"## {expected_heading}":
        return False
    text = section["content"].strip()
    if not text or span.get("text") != text:
        return False
    _heading_start, section_end_line = _section_line_bounds(sections, len(body_lines))[id(section)]
    raw_content_start_line = section["start_line"] + 1
    if raw_content_start_line > section_end_line:
        return False
    raw_content = "\n".join(body_lines[raw_content_start_line - 1 : section_end_line])
    leading_ws_len = len(raw_content) - len(raw_content.lstrip())
    line_start = raw_content_start_line + raw_content[:leading_ws_len].count("\n")
    line_end = line_start + text.count("\n")
    return (
        span.get("line_start") == line_start
        and span.get("line_end") == line_end
        and "\n".join(body_lines[line_start - 1 : line_end]) == text
    )


def _reject_forged_owner_anchor_source_spans(
    source_spans: Optional[dict],
    *,
    repo: Optional[str],
    issue_number: Optional[int],
    owner_anchor_snapshot: Optional[dict],
    field_labels: dict[str, str],
) -> Optional[dict]:
    """Drop unbound owner-anchor candidates, including list entries, before
    classification. A dropped sole candidate follows the established
    human-review-required path rather than introducing a new disposition."""
    if not source_spans:
        return source_spans
    filtered: dict = {}
    for field_id, entry in source_spans.items():
        if isinstance(entry, list):
            kept = [
                candidate
                for candidate in entry
                if not isinstance(candidate, dict)
                or _validate_owner_anchor_source_span_authority(
                    candidate,
                    field_id=field_id,
                    repo=repo,
                    issue_number=issue_number,
                    owner_anchor_snapshot=owner_anchor_snapshot,
                    field_labels=field_labels,
                )
            ]
            if kept:
                filtered[field_id] = kept
            continue
        if isinstance(entry, dict) and not _validate_owner_anchor_source_span_authority(
            entry,
            field_id=field_id,
            repo=repo,
            issue_number=issue_number,
            owner_anchor_snapshot=owner_anchor_snapshot,
            field_labels=field_labels,
        ):
            continue
        filtered[field_id] = entry
    return filtered


def parse_issue_template_fields(template_text: str, template_path: str) -> list[dict]:
    """Parse a GitHub issue-form YAML template (`.github/ISSUE_TEMPLATE/*.yml`)
    into ordered field metadata.

    Each returned dict carries: field_id, label, required, value, placeholder,
    order (0-indexed field-declaration order, `markdown` blocks excluded since
    they are not addressable contract fields), template_path, and
    template_digest (sha256 of the raw template file text — an immutable
    blob-identity binding for every field extracted from it, per AC1).
    """
    import yaml as _yaml

    template_digest = _sha256(template_text)
    doc = _yaml.safe_load(template_text) or {}
    body_items = doc.get("body") or []

    fields: list[dict] = []
    order = 0
    for item in body_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "markdown":
            continue  # informational block, not an addressable contract field
        field_id = item.get("id")
        if not field_id:
            continue
        attrs = item.get("attributes") or {}
        validations = item.get("validations") or {}
        fields.append({
            "field_id": field_id,
            "label": attrs.get("label", ""),
            "required": bool(validations.get("required", False)),
            "value": attrs.get("value"),
            "placeholder": attrs.get("placeholder"),
            "order": order,
            "template_path": template_path,
            "template_digest": template_digest,
        })
        order += 1
    return fields


def _parse_h2_sections(body: str) -> list[dict]:
    """Parse top-level (``## ``) CommonMark headings, fence-aware: a line that
    looks like a heading but occurs inside a fenced code block (```` ``` ````
    or ``~~~``) is NOT treated as a heading (so a body containing a literal
    ``## Foo`` inside an example fenced block is not misread).

    Returns an ordered list of ``{heading, start_line, content}`` (1-indexed
    start_line; content is the section body with leading/trailing whitespace
    stripped, excluding the heading line itself).
    """
    lines = body.split("\n")
    sections: list[dict] = []
    current: Optional[dict] = None
    in_fence = False
    fence_marker: Optional[tuple[str, int]] = None

    for idx, line in enumerate(lines):
        # Issue #995 fix_delta (P1-1): open/close are matched with DIFFERENT
        # regexes. A line can only CLOSE the currently-open fence if it
        # consists of nothing but (<=3 leading spaces + fence chars +
        # trailing spaces/tabs) -- CommonMark forbids trailing non-whitespace
        # (incl. an info string) on a closing fence line. Previously a single
        # regex matched a leading fence marker regardless of trailing text,
        # so a fenced code EXAMPLE containing a fake closer
        # (` ```not-a-close `) was treated as real, letting content after it
        # (e.g. a spoofed `## Outcome`) escape the code block and be parsed
        # as a real section.
        if in_fence:
            m_close = _FENCE_CLOSE_ONLY_RE.match(line)
            if m_close:
                marker_char = m_close.group(2)[0]
                marker_len = len(m_close.group(2))
                if (
                    fence_marker is not None
                    and marker_char == fence_marker[0]
                    and marker_len >= fence_marker[1]
                ):
                    in_fence = False
                    fence_marker = None
            if current is not None:
                current["content_lines"].append(line)
            continue

        m_open = _FENCE_OPEN_RE.match(line)
        if m_open:
            marker_char = m_open.group(2)[0]
            marker_len = len(m_open.group(2))
            in_fence = True
            fence_marker = (marker_char, marker_len)
            if current is not None:
                current["content_lines"].append(line)
            continue

        m = _H2_HEADING_RE.match(line)
        if m:
            if current is not None:
                sections.append(current)
            current = {
                "heading": m.group("heading").strip(),
                "start_line": idx + 1,
                "content_lines": [],
            }
            continue

        if current is not None:
            current["content_lines"].append(line)

    if current is not None:
        sections.append(current)

    for section in sections:
        section["content"] = "\n".join(section["content_lines"]).strip()
        del section["content_lines"]
    return sections


# Issue #995 fix_delta (P0-5): discriminated MRC parse result -- fail-closed
# statuses only, never a bare ``None`` that a caller could mistake for "no
# defect" and silently skip the required-key check for.
MRC_PARSE_STATUS_OK = "ok"
MRC_PARSE_STATUS_MISSING = "missing"
MRC_PARSE_STATUS_MALFORMED = "malformed"
MRC_PARSE_STATUS_DUPLICATE_KEY = "duplicate_key"
MRC_PARSE_STATUS_AMBIGUOUS = "ambiguous"


def _mrc_parse(body: str) -> dict:
    """Parse the ``## Machine-Readable Contract`` section using the
    canonical, section-bound, duplicate-key-rejecting parser
    (mrc_contract_parser.parse_machine_readable_contract, Issue #1135 SSOT).

    Returns ``{"status": ..., "keys": dict, "errors": [str, ...]}`` where
    status is one of the closed MRC_PARSE_STATUS_* values above. `status`
    is NEVER silently treated as "no defect" by callers -- every non-"ok"
    status routes to human_review_required (Issue #995 P0-5)."""
    if _canonical_parse_mrc is None:
        # Fail-closed environment failure: the canonical parser dependency
        # itself could not be imported. Never fall back to a permissive
        # local reimplementation that could silently diverge from the SSOT.
        return {"status": MRC_PARSE_STATUS_MALFORMED, "keys": {}, "errors": ["mrc_parser_import_failed"]}

    result = _canonical_parse_mrc(body)
    if result.ok:
        return {"status": MRC_PARSE_STATUS_OK, "keys": dict(result.data or {}), "errors": []}

    reason = result.reason
    if reason == "mrc_section_missing":
        return {"status": MRC_PARSE_STATUS_MISSING, "keys": {}, "errors": [reason]}
    if reason == "duplicate_key":
        return {
            "status": MRC_PARSE_STATUS_DUPLICATE_KEY,
            "keys": {},
            "errors": [f"{reason}:{result.duplicate_key}"],
        }
    if reason in ("mrc_section_multiple", "mrc_yaml_fence_multiple"):
        return {"status": MRC_PARSE_STATUS_AMBIGUOUS, "keys": {}, "errors": [reason]}
    # mrc_yaml_fence_missing / mrc_yaml_syntax_error / mrc_root_not_mapping
    return {"status": MRC_PARSE_STATUS_MALFORMED, "keys": {}, "errors": [reason]}


def _is_syntactic_scalar(value: object) -> bool:
    """Guard for `derived_scalar_exact`: only a syntactically unique scalar
    (issue number, `none`, a single template-order-derived label, etc.) may
    ever be used — never prose, never a summary/interpretation. This is a
    generic fallback guard, weaker than a field-specific closed validator
    (`_DERIVED_SCALAR_FIELD_VALIDATORS`); used only for fields that do not
    have one (Issue #995 P0-3)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        s = value.strip()
        if not s or "\n" in s:
            return False
        return bool(re.fullmatch(r"#?\d+|none|N/A|[a-zA-Z][a-zA-Z0-9_/.\-]{0,63}", s))
    return False


def _classify_missing_field(
    field: dict,
    known_scalars: dict,
    source_spans: dict,
) -> dict:
    """Classify a single missing required field into the closed derivation
    enum (auto_apply_safe) or human_review_required (AC3/AC4/AC5)."""
    field_id = field["field_id"]

    value = field.get("value")
    has_exact_template_value = value is not None and str(value).strip() != ""
    if field_id in _TEMPLATE_VALUE_AUTO_SAFE_FIELD_IDS and has_exact_template_value:
        candidate = str(value)
        # Issue #995 fix_delta (P0-4): a non-empty template default is not
        # automatically a real value -- it can be an authoring placeholder
        # scaffold (angle-bracket hint / unselected enum). Only a
        # placeholder-free default may ever be template_value_exact.
        if not _contains_placeholder_scaffold(candidate):
            return {
                "disposition": STRUCT_DISPOSITION_AUTO_APPLY_SAFE,
                "derivation": DERIVATION_TEMPLATE_VALUE_EXACT,
                "candidate_value": candidate,
                "candidate_digest": _sha256(candidate),
                "reason_codes": ["template_default_value_exact"],
            }

    span_entry = source_spans.get(field_id)
    if isinstance(span_entry, list):
        if len(span_entry) > 1:
            return {
                "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                "derivation": None,
                "reason_codes": ["multiple_source_conflict"],
            }
        span_entry = span_entry[0] if span_entry else None
    if (
        isinstance(span_entry, dict)
        and isinstance(span_entry.get("text"), str)
        and span_entry["text"].strip() != ""
    ):
        # Issue #995 fix_delta (P0-3): a `text` field alone is no longer
        # sufficient. Full authority/source/span/digest provenance is
        # required before a source span can back an auto_apply_safe item.
        span_ok, span_reasons = _validate_source_span_provenance(span_entry)
        if span_ok:
            candidate = span_entry["text"]
            source_text_sha256 = _sha256(candidate)
            return {
                "disposition": STRUCT_DISPOSITION_AUTO_APPLY_SAFE,
                "derivation": DERIVATION_SOURCE_SPAN_EXACT,
                "candidate_value": candidate,
                "candidate_digest": source_text_sha256,
                "source_url": span_entry.get("source_url"),
                "source_span": {
                    "line_start": span_entry.get("line_start"),
                    "line_end": span_entry.get("line_end"),
                    "authority_kind": span_entry.get("authority_kind"),
                    "source_repo": span_entry.get("source_repo"),
                    "source_object_kind": span_entry.get("source_object_kind"),
                    "source_object_id": span_entry.get("source_object_id"),
                    "source_revision": span_entry.get("source_revision"),
                    "source_text_sha256": source_text_sha256,
                    "candidate_sha256": source_text_sha256,
                },
                "reason_codes": ["single_authoritative_source_span_with_provenance"],
            }
        return {
            "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
            "derivation": None,
            "reason_codes": span_reasons,
        }

    scalar = known_scalars.get(field_id)
    if scalar is not None:
        validator = _DERIVED_SCALAR_FIELD_VALIDATORS.get(field_id)
        if validator is not None:
            # Issue #995 fix_delta (P0-3): field-specific closed validator.
            # A field WITH a validator entry must match it, or it is
            # human_review_required -- it may NOT silently fall through to
            # the weaker generic syntactic guard below.
            if isinstance(scalar, str) and validator.match(scalar.strip()):
                candidate = scalar.strip()
                return {
                    "disposition": STRUCT_DISPOSITION_AUTO_APPLY_SAFE,
                    "derivation": DERIVATION_DERIVED_SCALAR_EXACT,
                    "candidate_value": candidate,
                    "candidate_digest": _sha256(candidate),
                    "reason_codes": ["field_specific_closed_validator_match"],
                }
            return {
                "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                "derivation": None,
                "reason_codes": ["derived_scalar_failed_field_specific_validator"],
            }
        if _is_syntactic_scalar(scalar):
            candidate = str(scalar)
            return {
                "disposition": STRUCT_DISPOSITION_AUTO_APPLY_SAFE,
                "derivation": DERIVATION_DERIVED_SCALAR_EXACT,
                "candidate_value": candidate,
                "candidate_digest": _sha256(candidate),
                "reason_codes": ["validated_syntactic_scalar_generic_guard"],
            }

    return {
        "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
        "derivation": None,
        "reason_codes": ["missing_required_field_without_exact_source"],
    }


def _classify_missing_contract_key(
    key: str,
    issue_kind: str,
    known_scalars: dict,
    source_spans: dict,
    template_path: str,
    template_digest: str,
    *,
    template_declared_schema_version: Optional[str] = None,
) -> dict:
    """Classify a missing/placeholder Machine-Readable Contract key."""
    if key == "contract_schema_version":
        # Issue #995 fix_delta (P0-4): derive from the template's OWN
        # committed scalar (`_extract_template_declared_scalar`) instead of
        # an independently hard-coded "v1" literal that could silently
        # diverge from a future template revision.
        if template_declared_schema_version:
            candidate = template_declared_schema_version
            return {
                "disposition": STRUCT_DISPOSITION_AUTO_APPLY_SAFE,
                "derivation": DERIVATION_TEMPLATE_VALUE_EXACT,
                "candidate_value": candidate,
                "candidate_digest": _sha256(candidate),
                "reason_codes": ["template_declared_schema_version_exact"],
            }
        return {
            "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
            "derivation": None,
            "reason_codes": ["contract_schema_version_not_resolvable_from_template"],
        }
    if key == "issue_kind":
        if issue_kind not in _CANONICAL_ISSUE_KINDS:
            return {
                "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                "derivation": None,
                "reason_codes": ["unknown_issue_kind"],
            }
        return {
            "disposition": STRUCT_DISPOSITION_AUTO_APPLY_SAFE,
            "derivation": DERIVATION_DERIVED_SCALAR_EXACT,
            "candidate_value": issue_kind,
            "candidate_digest": _sha256(issue_kind),
            "reason_codes": ["single_valid_issue_kind_scalar"],
        }
    synthetic_field = {"field_id": f"machine-readable-contract.{key}", "value": None}
    return _classify_missing_field(synthetic_field, known_scalars, source_spans)


# ---------------------------------------------------------------------------
# Insertion decision / insertion anchor (Issue #995 P0-2)
#
# A consumer applying a proposal must know WHERE to insert it, not just what
# to insert. `_apply_insertion_decision` never invents an anchor: it only
# ever anchors relative to a template-adjacent heading that is present
# EXACTLY ONCE in the body (an ambiguous/duplicate/absent neighbourhood
# forces `insertion.disposition: ambiguous`, which in turn forces the whole
# item to `human_review_required` -- consumer/producer separation means the
# consumer must never invent its own insertion policy, Issue #995 Outcome).
# ---------------------------------------------------------------------------


def _section_line_bounds(sections: list[dict], body_line_count: int) -> dict[int, tuple[int, int]]:
    """Map each section object's identity -> (start_line, end_line), where
    end_line is the line just before the next section (or end of body)."""
    ordered = sorted(sections, key=lambda s: s["start_line"])
    bounds: dict[int, tuple[int, int]] = {}
    for idx, sec in enumerate(ordered):
        end = ordered[idx + 1]["start_line"] - 1 if idx + 1 < len(ordered) else body_line_count
        bounds[id(sec)] = (sec["start_line"], end)
    return bounds


def _apply_insertion_decision(
    item: dict,
    template_fields: list[dict],
    heading_index: dict[str, list[dict]],
    body: str,
) -> dict:
    """Attach `item["insertion"]` (Issue #995 P0-2) and, when no unambiguous
    anchor exists, downgrade the item to human_review_required regardless of
    its prior derivation-based classification (an unanchorable auto-safe
    candidate is not safe to hand to a consumer)."""
    body_lines = body.split("\n")
    all_sections = [sec for lst in heading_index.values() for sec in lst]
    bounds = _section_line_bounds(all_sections, len(body_lines))

    is_mrc_key_item = (
        item["field_id"].startswith("machine-readable-contract.")
        and item["field_id"] != "machine-readable-contract"
    )
    rendered_heading = "## Machine-Readable Contract" if is_mrc_key_item else f"## {item['label']}"
    candidate_value = item.get("candidate_value")
    candidate_section_digest = _sha256(
        f"{rendered_heading}\n\n{candidate_value}\n" if candidate_value else f"{rendered_heading}\n"
    )
    reason_codes = list(item.get("reason_codes") or [])

    def _finalize(disposition: str, relation: Optional[str], **anchor_fields) -> dict:
        item["insertion"] = {
            "disposition": disposition,
            "relation": relation,
            "anchor_field_id": anchor_fields.get("anchor_field_id"),
            "anchor_heading": anchor_fields.get("anchor_heading"),
            "anchor_start_line": anchor_fields.get("anchor_start_line"),
            "anchor_digest": anchor_fields.get("anchor_digest"),
            "rendered_heading": rendered_heading,
            "candidate_section_digest": candidate_section_digest,
        }
        if disposition == "ambiguous" and item.get("disposition") != STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED:
            item["disposition"] = STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
            item["derivation"] = None
            item["reason_codes"] = [*reason_codes, "ambiguous_insertion_anchor"]
            for auto_safe_only_field in ("candidate_value", "candidate_digest", "source_url", "source_span"):
                item.pop(auto_safe_only_field, None)
        return item

    if "duplicate_heading" in reason_codes:
        return _finalize("ambiguous", "replace_section_content")

    norm_label = item["label"].strip().casefold() if not is_mrc_key_item else None
    if norm_label is not None:
        existing = heading_index.get(norm_label, [])
        if len(existing) == 1:
            sec = existing[0]
            start, _end = bounds.get(id(sec), (sec["start_line"], sec["start_line"]))
            return _finalize(
                "exact", "replace_section_content",
                anchor_field_id=item["field_id"], anchor_heading=sec["heading"],
                anchor_start_line=start, anchor_digest=_sha256(f"## {sec['heading']}"),
            )

    if is_mrc_key_item:
        # NOTE: this looks up the actual body H2 HEADING TEXT ("Machine-
        # Readable Contract", casefolded), NOT the hyphenated template
        # `field_id` -- they are different strings by construction.
        mrc_sections = heading_index.get("Machine-Readable Contract".casefold(), [])
        if len(mrc_sections) == 1:
            sec = mrc_sections[0]
            _start, end = bounds.get(id(sec), (sec["start_line"], sec["start_line"]))
            return _finalize(
                "exact", "insert_contract_key",
                anchor_field_id="machine-readable-contract", anchor_heading=sec["heading"],
                anchor_start_line=end, anchor_digest=_sha256(f"## {sec['heading']}"),
            )
        return _finalize("ambiguous", "insert_contract_key")

    order = item["template_field_order"]
    ordered_fields = sorted(template_fields, key=lambda f: f["order"])
    preceding = [f for f in ordered_fields if f["order"] < order]
    following = [f for f in ordered_fields if f["order"] > order]

    for f in reversed(preceding):
        cand = heading_index.get(f["label"].strip().casefold(), [])
        if len(cand) == 1:
            sec = cand[0]
            _start, end = bounds.get(id(sec), (sec["start_line"], sec["start_line"]))
            return _finalize(
                "exact", "after",
                anchor_field_id=f["field_id"], anchor_heading=sec["heading"],
                anchor_start_line=end, anchor_digest=_sha256(f"## {sec['heading']}"),
            )
    for f in following:
        cand = heading_index.get(f["label"].strip().casefold(), [])
        if len(cand) == 1:
            sec = cand[0]
            start, _end = bounds.get(id(sec), (sec["start_line"], sec["start_line"]))
            return _finalize(
                "exact", "before",
                anchor_field_id=f["field_id"], anchor_heading=sec["heading"],
                anchor_start_line=start, anchor_digest=_sha256(f"## {sec['heading']}"),
            )
    return _finalize("ambiguous", None)


def detect_missing_template_sections(
    body: str,
    *,
    issue_kind: str,
    template_text: str,
    template_path: str,
    known_scalars: Optional[dict] = None,
    source_spans: Optional[dict] = None,
) -> list[dict]:
    """Detect, in a single deterministic batch, every missing / duplicate /
    empty / placeholder-only required template-derived section and every
    missing/placeholder Machine-Readable Contract key (AC1/AC2/AC5).

    Returns items ordered by template field order, then field id (AC2).
    Heading set membership alone is never used to assert presence: a
    duplicate heading, an empty section, or a section containing only the
    template's authoring placeholder is still reported (AC5).
    """
    known_scalars = known_scalars or {}
    source_spans = source_spans or {}

    template_fields = parse_issue_template_fields(template_text, template_path)
    sections = _parse_h2_sections(body)

    heading_index: dict[str, list[dict]] = {}
    for section in sections:
        heading_index.setdefault(section["heading"].strip().casefold(), []).append(section)

    items: list[dict] = []

    for field in template_fields:
        if not field["required"]:
            continue
        norm_label = field["label"].strip().casefold()
        matches = heading_index.get(norm_label, [])
        observed_cardinality = len(matches)

        base_item = {
            "field_id": field["field_id"],
            "label": field["label"],
            "required": True,
            "template_field_order": field["order"],
            "template_path": field["template_path"],
            "template_digest": field["template_digest"],
            "expected_cardinality": 1,
            "observed_cardinality": observed_cardinality,
        }

        if observed_cardinality > 1:
            items.append({
                **base_item,
                "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                "derivation": None,
                "reason_codes": ["duplicate_heading"],
            })
            continue

        if observed_cardinality == 1:
            content = matches[0]["content"]
            placeholder = field.get("placeholder")
            is_empty = content == ""
            is_placeholder_only = bool(placeholder) and content.strip() == str(placeholder).strip()
            is_required_token = bool(_REQUIRED_PLACEHOLDER_RE.match(content.strip()))
            if is_empty or is_placeholder_only or is_required_token:
                # Issue #995 fix_delta (P1-3): a heading that is genuinely
                # PRESENT once (but empty/placeholder-only) must keep
                # observed_cardinality == 1 -- overwriting it to 0
                # conflated "this content is not usable" with "this heading
                # was never observed", which the OWNER review flagged as
                # confusing observed fact with content-state judgment.
                items.append({
                    **base_item,
                    "content_state": "empty" if is_empty else "placeholder",
                    "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                    "derivation": None,
                    "reason_codes": ["empty_or_placeholder_only_section"],
                })
            continue

        items.append({
            **base_item,
            **_classify_missing_field(field, known_scalars, source_spans),
        })

    # Issue #995 fix_delta (P0-5): the MRC parse result is a discriminated,
    # fail-closed status -- "ok" is the ONLY status that skips the
    # section-missing/malformed handling below. Every other status still
    # enumerates every required contract key for `issue_kind` as
    # human_review_required (never silently skipped, unlike the old
    # `if mrc_keys is not None:` fail-open guard).
    mrc_result = _mrc_parse(body)
    required_keys = REQUIRED_CONTRACT_KEYS_BY_KIND.get(issue_kind)
    template_digest = _sha256(template_text)
    mrc_template_field = next(
        (f for f in template_fields if f["field_id"] == "machine-readable-contract"), None
    )
    template_declared_schema_version = _extract_template_declared_scalar(
        mrc_template_field.get("value") if mrc_template_field else None,
        "contract_schema_version",
    )

    def _mrc_key_item(key: str, *, observed_cardinality: int, classification: dict) -> dict:
        return {
            "field_id": f"machine-readable-contract.{key}",
            "label": f"Machine-Readable Contract: {key}",
            "required": True,
            "template_field_order": -1,
            "template_path": template_path,
            "template_digest": template_digest,
            "expected_cardinality": 1,
            "observed_cardinality": observed_cardinality,
            **classification,
        }

    if required_keys is None:
        # Issue #995 fix_delta (P0-5): an issue_kind outside the SSOT-parsed
        # closed enum can never resolve a required-key set -- fail closed
        # rather than silently using an empty list (which previously made
        # `for key in required_keys` a no-op).
        items.append(_mrc_key_item(
            "<unresolved>",
            observed_cardinality=0,
            classification={
                "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                "derivation": None,
                "reason_codes": ["unknown_issue_kind"],
            },
        ))
    elif mrc_result["status"] == MRC_PARSE_STATUS_OK:
        mrc_keys = mrc_result["keys"]
        observed_issue_kind = mrc_keys.get("issue_kind")
        if (
            isinstance(observed_issue_kind, str)
            and observed_issue_kind.strip()
            and observed_issue_kind.strip() != issue_kind
        ):
            # Issue #995 fix_delta (P0-5): the MRC's own `issue_kind` value
            # disagreeing with the trusted resolved issue_kind is an
            # authority conflict, never silently auto-safe.
            items.append(_mrc_key_item(
                "issue_kind",
                observed_cardinality=1,
                classification={
                    "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                    "derivation": None,
                    "reason_codes": ["issue_kind_authority_conflict"],
                },
            ))
        for key in required_keys:
            is_missing = key not in mrc_keys
            value = mrc_keys.get(key)
            is_null_or_empty = (not is_missing) and (
                value is None or (isinstance(value, str) and value.strip() == "")
            )
            is_placeholder = isinstance(value, str) and (
                bool(_REQUIRED_PLACEHOLDER_RE.match(value.strip()))
                or _is_placeholder_mrc_scalar(
                    key,
                    value,
                    _extract_template_mrc_default(
                        mrc_template_field.get("value") if mrc_template_field else None,
                        key,
                    ),
                )
            )
            if not (is_missing or is_null_or_empty or is_placeholder):
                continue
            items.append(_mrc_key_item(
                key,
                observed_cardinality=0 if (is_missing or is_null_or_empty) else 1,
                classification=_classify_missing_contract_key(
                    key, issue_kind, known_scalars, source_spans, template_path, template_digest,
                    template_declared_schema_version=template_declared_schema_version,
                ),
            ))
    elif mrc_result["status"] == MRC_PARSE_STATUS_MISSING:
        for key in required_keys:
            items.append(_mrc_key_item(
                key,
                observed_cardinality=0,
                classification={
                    "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                    "derivation": None,
                    "reason_codes": ["mrc_section_missing"],
                },
            ))
    else:
        # malformed / duplicate_key / ambiguous: fail closed for every
        # required key of this issue_kind (Issue #995 P0-5).
        for key in required_keys:
            items.append(_mrc_key_item(
                key,
                observed_cardinality=0,
                classification={
                    "disposition": STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED,
                    "derivation": None,
                    "reason_codes": [mrc_result["status"], *mrc_result["errors"]],
                },
            ))

    items = [
        _apply_insertion_decision(item, template_fields, heading_index, body)
        for item in items
    ]
    items.sort(key=lambda i: (i["template_field_order"], i["field_id"]))
    return items


def build_structural_repair_bundle(
    body: str,
    *,
    issue_kind: str,
    template_text: str,
    template_path: str,
    repo: Optional[str] = None,
    issue_number: Optional[int] = None,
    original_updated_at: Optional[str] = None,
    known_scalars: Optional[dict] = None,
    source_spans: Optional[dict] = None,
    owner_anchor_snapshot: Optional[dict] = None,
    template_git_blob_sha: Optional[str] = None,
    template_source_ref: Optional[str] = None,
) -> dict:
    """Produce the single versioned handoff bundle for one producer run
    (AC2/AC6/AC7). Pure Python string/YAML processing only: never invokes
    `gh`/GitHub REST/GraphQL, never writes the Issue body, never dispatches
    items individually — #2039 (out of scope here) owns consumption.

    `template_git_blob_sha`/`template_source_ref` (Issue #995 fix_delta
    P1-3, OWNER adversarial review) are OPTIONAL, additive provenance the
    CALLER supplies -- this module stays pure and never shells out to `git`
    itself. `template_digest` (each item's content SHA-256 of
    `template_text`, unchanged) still binds a template's exact BYTES;
    these two new top-level fields additionally bind the template FILE's
    git blob identity and the repo@commit ref the caller resolved it from,
    so a caller with git/GitHub access (e.g. run_refinement_preflight.py)
    can prove the template text actually came from a trusted repository
    ref rather than an arbitrary/tampered local file. Both are `None` when
    the caller does not supply them (e.g. the standalone unit tests in
    this repo, which construct `template_text` in-memory with no git blob
    to bind to).
    """
    original_body_sha256 = _sha256(body)
    # PR #2469 fix_delta iteration 3 (P1-2): reject any forged `current_issue`
    # source span BEFORE the classifier ever sees it -- see
    # `_reject_forged_current_issue_source_spans()`'s docstring for why this
    # generic producer boundary (not the assembler) is where this belongs.
    source_spans = _reject_forged_current_issue_source_spans(
        source_spans, repo=repo, issue_number=issue_number,
        original_body_sha256=original_body_sha256,
    )
    # Issue #2582: unlike `current_issue`, an owner-anchor source lives in a
    # separate comment. Bind its span to the independently supplied, already
    # trusted snapshot before the common classifier reads any candidate.
    field_labels = {
        field["field_id"]: field["label"]
        for field in parse_issue_template_fields(template_text, template_path)
    }
    source_spans = _reject_forged_owner_anchor_source_spans(
        source_spans,
        repo=repo,
        issue_number=issue_number,
        owner_anchor_snapshot=owner_anchor_snapshot,
        field_labels=field_labels,
    )
    items = detect_missing_template_sections(
        body,
        issue_kind=issue_kind,
        template_text=template_text,
        template_path=template_path,
        known_scalars=known_scalars,
        source_spans=source_spans,
    )
    for item in items:
        item["repo"] = repo
        item["issue_number"] = issue_number
        item["original_body_sha256"] = original_body_sha256
        item["original_updated_at"] = original_updated_at

    if not items:
        disposition_summary = "no_missing_fields_detected"
    elif any(i["disposition"] == STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED for i in items):
        disposition_summary = STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED
    else:
        disposition_summary = STRUCT_DISPOSITION_AUTO_APPLY_SAFE

    return {
        "schema_version": STRUCTURAL_REPAIR_SCHEMA_VERSION,
        "policy_version": STRUCTURAL_REPAIR_POLICY_VERSION,
        "issue_kind": issue_kind,
        "repo": repo,
        "issue_number": issue_number,
        "original_body_sha256": original_body_sha256,
        "original_updated_at": original_updated_at,
        "items": items,
        "disposition_summary": disposition_summary,
        "template_git_blob_sha": template_git_blob_sha,
        "template_source_ref": template_source_ref,
    }



# ---------------------------------------------------------------------------
# structural_repair_action -> control-plane routing (Issue #995 fix_delta P0-1)
#
# Mirrors `classify_repair_action()`'s producer-side role for the
# body-defect repair lane: a single, versioned, closed-enum function that
# maps `structural_repair_action.disposition_summary` to the SAME
# status/next_action vocabulary `run_refinement_preflight.py` already uses
# for `repair_action` (needs_fix / apply_deterministic_repair, blocked /
# human_judgment_required). This is the producer-side half of "connect
# structural_repair_action to control-plane routing" -- the OWNER's P0-1
# concrete requirement ("disposition_summary=auto_apply_safe -> needs_fix",
# "human_review_required -> blocked", "no_missing_fields_detected -> pass/
# warn only") is implemented here as a pure, independently unit-tested
# function so a wrapper can never emit a `status: pass` / `next_action:
# proceed` result alongside a structural_repair_action that disagrees.
#
# NOTE (scope disclosure, updated for the P0-1 wiring fix_delta commit):
# `run_refinement_preflight.py` DOES now call `build_structural_repair_bundle()`
# and `route_structural_repair_disposition()` from its production preflight
# path (see `_resolve_structural_repair_template()` and the "Structural
# (template-derived) repair pass" block in that module). The call remains
# CONDITIONAL, not unconditional: it only fires when the Issue body carries a
# locally-resolvable self-declared `issue_kind:` scalar that
# `_resolve_structural_repair_template()` can match to one of the closed
# {parent, implementation, research} kinds and their checked-in
# `.github/ISSUE_TEMPLATE/*.yml` file -- this stays a purely local, no-GitHub
# -call resolution, never a live-Issue-metadata fetch. When no such
# self-declared kind is resolvable, `structural_repair_action` stays absent
# for that run (schema-documented as "absent when the producer was not
# invoked in structural-repair mode") rather than guessing. What IS wired:
# (1) this routing function, called from the production path above, (2) a
# schema-level `allOf` invariant in refinement_preflight_result_v1.schema.json
# that fails closed if a result ever carries `status: pass` alongside a
# `structural_repair_action` whose `disposition_summary` is not
# `no_missing_fields_detected` -- the exact contradiction the OWNER's P0-1
# example showed.
# ---------------------------------------------------------------------------

STRUCTURAL_REPAIR_ROUTE_STATUS_PASS = "pass"
STRUCTURAL_REPAIR_ROUTE_STATUS_NEEDS_FIX = "needs_fix"
STRUCTURAL_REPAIR_ROUTE_STATUS_BLOCKED = "blocked"


def route_structural_repair_disposition(structural_repair_action: dict) -> dict:
    """Closed-enum routing for a `structural_repair_action` bundle
    (Issue #995 P0-1). Returns
    ``{"status": ..., "next_action": ..., "reason_codes": [...]}``.

    Routing table (never silently defaulted to pass/proceed):
      disposition_summary == "auto_apply_safe"          -> needs_fix / apply_deterministic_structural_repair
      disposition_summary == "human_review_required"     -> blocked / human_judgment_required
      disposition_summary == "no_missing_fields_detected" -> pass / proceed
      anything else (malformed bundle)                    -> blocked / human_judgment_required
    """
    if not isinstance(structural_repair_action, dict):
        return {
            "status": STRUCTURAL_REPAIR_ROUTE_STATUS_BLOCKED,
            "next_action": "human_judgment_required",
            "reason_codes": ["structural_repair_action_not_an_object"],
        }
    disposition_summary = structural_repair_action.get("disposition_summary")
    items = structural_repair_action.get("items")
    if disposition_summary == "no_missing_fields_detected":
        return {
            "status": STRUCTURAL_REPAIR_ROUTE_STATUS_PASS,
            "next_action": "proceed",
            "reason_codes": ["no_missing_fields_detected"],
        }
    if disposition_summary == STRUCT_DISPOSITION_AUTO_APPLY_SAFE:
        if not isinstance(items, list) or not items or any(
            not isinstance(i, dict) or i.get("disposition") != STRUCT_DISPOSITION_AUTO_APPLY_SAFE
            for i in items
        ):
            # Schema-invariant violation surfaced as a routing decision too
            # (defence in depth): a summary claiming auto_apply_safe MUST be
            # backed by >=1 item that is ITSELF auto_apply_safe, and no item
            # may contradict the summary.
            return {
                "status": STRUCTURAL_REPAIR_ROUTE_STATUS_BLOCKED,
                "next_action": "human_judgment_required",
                "reason_codes": ["disposition_summary_items_mismatch"],
            }
        return {
            "status": STRUCTURAL_REPAIR_ROUTE_STATUS_NEEDS_FIX,
            "next_action": "apply_deterministic_structural_repair",
            "reason_codes": ["structural_auto_apply_safe_items_present"],
        }
    if disposition_summary == STRUCT_DISPOSITION_HUMAN_REVIEW_REQUIRED:
        return {
            "status": STRUCTURAL_REPAIR_ROUTE_STATUS_BLOCKED,
            "next_action": "human_judgment_required",
            "reason_codes": ["structural_human_review_required_items_present"],
        }
    return {
        "status": STRUCTURAL_REPAIR_ROUTE_STATUS_BLOCKED,
        "next_action": "human_judgment_required",
        "reason_codes": [f"unknown_disposition_summary:{disposition_summary!r}"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic repair pass for Issue contract defects. "
            "Dry-run by default; use --apply to write changes."
        )
    )
    parser.add_argument("--body-file", required=True, help="Path to Issue body file")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply repairs and write to --out-file",
    )
    parser.add_argument(
        "--out-file",
        default=None,
        help="Output path for repaired body (required when --apply is given)",
    )
    parser.add_argument(
        "--artifact-root",
        default=None,
        help=(
            "Allowed artifact root for candidate materialization containment "
            "verification (Issue #2016 iteration-3 P0-2). Optional."
        ),
    )

    args = parser.parse_args(argv)

    # Read body
    body_path = Path(args.body_file)
    if not body_path.exists():
        result = {
            "schema": SCHEMA,
            "dry_run": not args.apply,
            "changed": False,
            "original_body_sha256": "sha256:",
            "repaired_body_sha256": "sha256:",
            "repairs": [],
            "error": f"body_file_not_found: {args.body_file}",
        }
        print(json.dumps(result, indent=2))
        return 1

    try:
        body = body_path.read_text(encoding="utf-8")
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "dry_run": not args.apply,
            "changed": False,
            "original_body_sha256": "sha256:",
            "repaired_body_sha256": "sha256:",
            "repairs": [],
            "error": f"body_read_error: {exc}",
        }
        print(json.dumps(result, indent=2))
        return 1

    if args.apply and not args.out_file:
        print(
            json.dumps({
                "schema": SCHEMA,
                "dry_run": False,
                "changed": False,
                "original_body_sha256": "sha256:",
                "repaired_body_sha256": "sha256:",
                "repairs": [],
                "error": "--out-file is required when --apply is given",
            }, indent=2)
        )
        return 1

    try:
        result = run_repair(body, apply=args.apply, out_file=args.out_file, root_dir=args.artifact_root)
    except CandidateWriteSecurityError as exc:
        result = {
            "schema": SCHEMA,
            "dry_run": not args.apply,
            "changed": False,
            "original_body_sha256": "sha256:",
            "repaired_body_sha256": "sha256:",
            "repairs": [],
            "error": f"candidate_write_security_error: {exc}",
        }
        print(json.dumps(result, indent=2))
        return 1
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "dry_run": not args.apply,
            "changed": False,
            "original_body_sha256": "sha256:",
            "repaired_body_sha256": "sha256:",
            "repairs": [],
            "error": f"repair_error: {exc}",
        }
        print(json.dumps(result, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
