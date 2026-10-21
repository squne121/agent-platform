#!/usr/bin/env python3
"""
scope_signal_delta.py

Deterministic read-only CLI and library for scope signal delta analysis.

Input (stdin JSON):
  SCOPE_SIGNAL_DELTA_INPUT_V1

Output (stdout JSON):
  SCOPE_SIGNAL_DELTA_V1
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

# #2048 regression follow-up (#1877/PR #1884): decide_scope_reframe_contract_route()
# lives in the sibling decide_rewrite_route.py module and is consumed here so an
# approved trusted-anchor scope reframe whose derived operations[] is empty is
# routed to issue_editor_required from the actual production call path
# (run_trusted_anchor_iteration_zero(), invoked by run_refinement_preflight.py's
# contract_update.run.with_anchor lane) instead of only existing as an
# unreferenced unit-tested function.
try:
    from decide_rewrite_route import (
        SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1,
        decide_scope_reframe_contract_route,
        classify_scope_reframe_disposition,
        ROUTE_ISSUE_EDITOR_REQUIRED,
    )
    _SCOPE_REFRAME_ROUTER_IMPORT_FAILED = False
except ImportError:  # pragma: no cover - sibling module should always be importable
    SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1 = None
    decide_scope_reframe_contract_route = None
    classify_scope_reframe_disposition = None
    ROUTE_ISSUE_EDITOR_REQUIRED = "issue_editor_required"
    # PR #2057 OWNER review P2: a safety-critical routing dependency import
    # failure must surface as `environment_failure`, never as an ordinary
    # no-change/no-op continuation. `run_trusted_anchor_iteration_zero()`
    # checks this flag and fails closed instead of silently omitting the
    # `rewrite_route` annotation.
    _SCOPE_REFRAME_ROUTER_IMPORT_FAILED = True

SCHEMA_VERSION = "scope_signal_delta/v1"

REASON_NEW_IN_SCOPE = "new_in_scope_area"
REASON_NEW_ALLOWED_PATH_LAYER = "new_allowed_path_layer"
REASON_NEW_UNVERIFIABLE_AC = "new_unverifiable_ac"
REASON_NO_SCOPE_SIGNAL = "no_scope_signal"

SUBJECTIVE_KEYWORDS = (
    "適切に",
    "品質を改善",
    "安定する",
    "改善する",
    "最適化",
    "高品質に",
    "improve",
    "enhance",
    "optimize",
    "stabilize",
    "appropriately",
)

PATH_TOKEN_RE = re.compile(r"`(?P<path>[^`\n]+)`|(?P<bare>(?:\.claude|docs|src|scripts|tests|\.github)/[^\s|`]+)")
HEADING_RE = re.compile(r"^[ ]{0,3}##[ \t]+(?P<title>.+?)[ \t#]*$")

INPUT_REQUIRED_FIELDS = ("before_body", "current_body", "after_body", "source_refs")
INPUT_SOURCE_REF_KEYS = ("before", "current", "after")


@dataclass(frozen=True)
class SourceLine:
    number: int
    text: str


def _leading_space_count(line: str) -> int:
    count = 0
    for ch in line:
        if ch == " ":
            count += 1
        else:
            break
    return count


def _parse_fence_opener(line: str) -> tuple[str, int] | None:
    if _leading_space_count(line) > 3:
        return None
    content = line.lstrip(" ")
    if not content or content[0] not in ("`", "~"):
        return None
    fence_char = content[0]
    run_len = 0
    for ch in content:
        if ch == fence_char:
            run_len += 1
        else:
            break
    if run_len < 3:
        return None
    return fence_char, run_len


def _is_fence_closer(line: str, fence_char: str, fence_len: int) -> bool:
    if _leading_space_count(line) > 3:
        return False
    content = line.lstrip(" ").rstrip()
    if not content or any(ch != fence_char for ch in content):
        return False
    return len(content) >= fence_len


def _parse_heading(line: str) -> str | None:
    if _leading_space_count(line) > 3:
        return None
    match = HEADING_RE.match(line.rstrip())
    if not match:
        return None
    return match.group("title").strip()


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def extract_sections(text: str, *, semantic_only: bool = False) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_content: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in text.splitlines():
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                if current_section is not None and not semantic_only:
                    current_content.append(line)
                continue
        else:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section is not None and not semantic_only:
                    current_content.append(line)
                continue
            if current_section is not None and not semantic_only:
                current_content.append(line)
            continue

        heading = _parse_heading(line)
        if heading is not None:
            if current_section is not None:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = heading
            current_content = []
        elif current_section is not None:
            current_content.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def _extract_sections(text: str) -> dict[str, str]:
    return extract_sections(text)


def _find_section_line_offset(text: str, section_name: str) -> int:
    in_fence = False
    fence_char = ""
    fence_len = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                continue
        else:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0
            continue

        heading = _parse_heading(line)
        if heading == section_name:
            return index + 1
    return 1


def iter_section_lines(
    text: str,
    section_name: str,
    *,
    semantic_only: bool = False,
) -> list[SourceLine]:
    lines: list[SourceLine] = []
    current_section: str | None = None
    in_fence = False
    fence_char = ""
    fence_len = 0

    for index, line in enumerate(text.splitlines(), start=1):
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                if current_section == section_name and not semantic_only:
                    lines.append(SourceLine(index, line))
                continue
        else:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section == section_name and not semantic_only:
                    lines.append(SourceLine(index, line))
                continue
            if current_section == section_name and not semantic_only:
                lines.append(SourceLine(index, line))
            continue

        heading = _parse_heading(line)
        if heading is not None:
            current_section = heading
            continue
        if current_section == section_name:
            lines.append(SourceLine(index, line))

    return lines


def _iter_section_lines(text: str, section_name: str) -> list[SourceLine]:
    return iter_section_lines(text, section_name)


def _normalize_path(path: str) -> str:
    normalized = path.strip().strip("`").strip()
    normalized = normalized.rstrip("/")
    return normalized.replace("\\", "/")


def _extract_path_items(text: str, section_name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    for source_line in iter_section_lines(text, section_name, semantic_only=True):
        raw_line = source_line.text
        for match in PATH_TOKEN_RE.finditer(raw_line):
            candidate = match.group("path") or match.group("bare") or ""
            normalized = _normalize_path(candidate)
            if not normalized:
                continue
            items.append(
                {
                    "value": normalized,
                    "start_line": source_line.number,
                    "end_line": source_line.number,
                    "text_sha256": _sha256(raw_line),
                }
            )
    return items


def _extract_in_scope_layers(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    prefixes = (".claude/", "docs/", "src/", "scripts/", "tests/", ".github/")

    for source_line in iter_section_lines(text, "In Scope", semantic_only=True):
        raw_line = source_line.text

        # Note: a single path token (e.g. a backtick-quoted or bare path) may
        # contain more than one of the known prefixes as an embedded
        # substring (for example ".claude/skills/<skill>/tests/<file>.py"
        # contains both ".claude/" and "tests/"). Counting that as two
        # independent layer mentions is a false positive (Issue #1327). We
        # instead extract whole path-like tokens via PATH_TOKEN_RE and only
        # attribute a prefix to a token when the token itself *starts with*
        # that prefix, so a prefix appearing mid-token never counts as an
        # extra layer.
        line_prefixes: set[str] = set()
        for match in PATH_TOKEN_RE.finditer(raw_line):
            candidate = _normalize_path(match.group("path") or match.group("bare") or "")
            for prefix in prefixes:
                if candidate.startswith(prefix):
                    line_prefixes.add(prefix)
                    break
        for prefix in prefixes:
            if prefix in line_prefixes:
                items.append(
                    {
                        "value": prefix.rstrip("/"),
                        "start_line": source_line.number,
                        "end_line": source_line.number,
                        "text_sha256": _sha256(raw_line),
                    }
                )
    return items


def _extract_ac_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_line in iter_section_lines(text, "Acceptance Criteria", semantic_only=True):
        raw_line = source_line.text
        stripped = raw_line.lstrip()
        if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
            normalized = re.sub(r"\s+", " ", stripped).strip()
            items.append(
                {
                    "value": normalized,
                    "start_line": source_line.number,
                    "end_line": source_line.number,
                    "text_sha256": _sha256(raw_line),
                    "is_low_verifiability": any(keyword in raw_line for keyword in SUBJECTIVE_KEYWORDS),
                }
            )
    return items


def _to_value_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    value_map: dict[str, dict[str, Any]] = {}
    for item in items:
        value_map.setdefault(item["value"], item)
    return value_map


def _top_level_layer(path: str) -> str | None:
    if "/" not in path:
        return None
    return path.split("/", 1)[0]


def _triggering_lines(
    body_version: str,
    source_ref: str | None,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "body_version": body_version,
            "source_ref": source_ref,
            "coordinate_space": "body_absolute_1_based",
            "start_line": item["start_line"],
            "end_line": item["end_line"],
            "text_sha256": item["text_sha256"],
        }
        for item in items
    ]


def compute_scope_signal_delta(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _validate_input(payload)
    before_body = payload["before_body"]
    current_body = payload["current_body"]
    after_body = payload["after_body"]
    source_refs = payload.get("source_refs") or {}

    before_allowed = _extract_path_items(before_body, "Allowed Paths")
    current_allowed = _extract_path_items(current_body, "Allowed Paths")
    after_allowed = _extract_path_items(after_body, "Allowed Paths")

    before_allowed_map = _to_value_map(before_allowed)
    current_allowed_map = _to_value_map(current_allowed)
    after_allowed_map = _to_value_map(after_allowed)

    before_in_scope = _extract_in_scope_layers(before_body)
    after_in_scope = _extract_in_scope_layers(after_body)
    before_in_scope_map = _to_value_map(before_in_scope)
    after_in_scope_map = _to_value_map(after_in_scope)

    before_ac = _extract_ac_items(before_body)
    after_ac = _extract_ac_items(after_body)
    before_ac_map = _to_value_map(before_ac)
    after_ac_map = _to_value_map(after_ac)

    before_allowed_values = set(before_allowed_map)
    current_allowed_values = set(current_allowed_map)
    after_allowed_values = set(after_allowed_map)
    added_allowed_values = sorted(after_allowed_values - before_allowed_values)
    removed_allowed_values = sorted(before_allowed_values - after_allowed_values)
    repeated_allowed_values = sorted(after_allowed_values & before_allowed_values)

    before_allowed_layers = sorted(
        layer for layer in {_top_level_layer(value) for value in before_allowed_values} if layer
    )
    current_allowed_layers = sorted(
        layer for layer in {_top_level_layer(value) for value in current_allowed_values} if layer
    )
    after_allowed_layers = sorted(
        layer for layer in {_top_level_layer(value) for value in after_allowed_values} if layer
    )
    added_allowed_layers = sorted(set(after_allowed_layers) - set(before_allowed_layers))
    removed_allowed_layers = sorted(set(before_allowed_layers) - set(after_allowed_layers))
    repeated_allowed_layers = sorted(set(after_allowed_layers) & set(before_allowed_layers))

    before_in_scope_values = set(before_in_scope_map)
    after_in_scope_values = set(after_in_scope_map)
    added_in_scope_layers = sorted(after_in_scope_values - before_in_scope_values)
    repeated_in_scope_layers = sorted(after_in_scope_values & before_in_scope_values)

    added_low_verifiability = [
        item["value"] for item in after_ac if item["value"] not in before_ac_map and item["is_low_verifiability"]
    ]

    signals: list[dict[str, Any]] = []

    low_verifiability_items = [after_ac_map[value] for value in added_low_verifiability]
    signals.append(
        {
            "reason_code": REASON_NEW_UNVERIFIABLE_AC,
            "triggered": bool(low_verifiability_items),
            "normalized_value": added_low_verifiability,
            "triggering_lines": _triggering_lines("after", source_refs.get("after"), low_verifiability_items),
        }
    )

    added_allowed_items = [
        after_allowed_map[value]
        for value in added_allowed_values
        if _top_level_layer(value) in set(added_allowed_layers)
    ]
    signals.append(
        {
            "reason_code": REASON_NEW_ALLOWED_PATH_LAYER,
            "triggered": bool(added_allowed_layers),
            "normalized_value": added_allowed_layers,
            "triggering_lines": _triggering_lines("after", source_refs.get("after"), added_allowed_items),
        }
    )

    added_in_scope_items = [after_in_scope_map[value] for value in added_in_scope_layers]
    signals.append(
        {
            "reason_code": REASON_NEW_IN_SCOPE,
            "triggered": bool(added_in_scope_layers),
            "normalized_value": added_in_scope_layers,
            "triggering_lines": _triggering_lines("after", source_refs.get("after"), added_in_scope_items),
        }
    )

    legacy_reason = REASON_NO_SCOPE_SIGNAL
    legacy_triggered = False
    selected_triggering_lines: list[dict[str, Any]] = []
    for reason_code in (
        REASON_NEW_UNVERIFIABLE_AC,
        REASON_NEW_ALLOWED_PATH_LAYER,
        REASON_NEW_IN_SCOPE,
    ):
        signal = next(item for item in signals if item["reason_code"] == reason_code)
        if signal["triggered"]:
            legacy_reason = reason_code
            legacy_triggered = True
            selected_triggering_lines = signal["triggering_lines"]
            break

    result = {
        "schema_version": SCHEMA_VERSION,
        "inputs": {
            "before_body_sha256": _sha256(before_body),
            "current_body_sha256": _sha256(current_body),
            "after_body_sha256": _sha256(after_body),
            "source_refs": {
                "before": source_refs.get("before"),
                "current": source_refs.get("current"),
                "after": source_refs.get("after"),
            },
        },
        "sections": {
            "allowed_paths": {
                "before": sorted(before_allowed_values),
                "current": sorted(current_allowed_values),
                "after": sorted(after_allowed_values),
                "added": added_allowed_values,
                "removed": removed_allowed_values,
                "repeated_existing": repeated_allowed_values,
                "before_layers": before_allowed_layers,
                "current_layers": current_allowed_layers,
                "after_layers": after_allowed_layers,
                "added_layers": added_allowed_layers,
                "removed_layers": removed_allowed_layers,
                "repeated_existing_layers": repeated_allowed_layers,
            },
            "in_scope": {
                "before_layers": sorted(before_in_scope_values),
                "after_layers": sorted(after_in_scope_values),
                "added_layers": added_in_scope_layers,
                "repeated_existing_layers": repeated_in_scope_layers,
            },
            "acceptance_criteria": {
                "added_low_verifiability_items": added_low_verifiability,
                "before": sorted(before_ac_map),
                "after": sorted(after_ac_map),
            },
        },
        "signals": signals,
        "legacy_scope_signal_guard": {
            "triggered": legacy_triggered,
            "reason_code": legacy_reason,
            "excluded_by_anchor_reframe": False,
            "triggering_lines": selected_triggering_lines,
        },
        "suppressions": {
            "anchor_reframe": {
                "status": "not_applicable",
                "implementation_go": False,
                "required_rerun": [],
            }
        },
    }
    return result


def _validate_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("input must be an object")
    unknown_fields = sorted(set(payload) - set(INPUT_REQUIRED_FIELDS))
    if unknown_fields:
        raise ValueError(f"unknown input fields: {', '.join(unknown_fields)}")
    for field in ("before_body", "current_body", "after_body"):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"{field} must be a string")
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, dict):
        raise ValueError("source_refs must be an object")
    unknown_source_ref_keys = sorted(set(source_refs) - set(INPUT_SOURCE_REF_KEYS))
    if unknown_source_ref_keys:
        raise ValueError(f"unknown source_refs fields: {', '.join(unknown_source_ref_keys)}")
    for key in INPUT_SOURCE_REF_KEYS:
        if key not in source_refs:
            raise ValueError(f"source_refs.{key} is required")
        value = source_refs.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"source_refs.{key} must be a string or null")
    return payload


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = compute_scope_signal_delta(_validate_input(payload))
        print(_canonical_json(result))
        return 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid_json: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - fail-closed CLI guard
        print(json.dumps({"error": f"internal_error: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# SCOPE_DELTA_AUTHORITY_V1 (#1323) -- classify scope delta authority
# (ai_inferred / human_review_directive / existing_parent_contract /
# related_issue_dependency), additive to SCOPE_SIGNAL_GUARD_DECISION_V2
# (#1090). Supersedes #1008 (trusted anchor scope amendment) and #1011
# (CONTEXT_PROVENANCE_V1) by folding both gaps into a single decision shape.
# ---------------------------------------------------------------------------

SCOPE_DELTA_AUTHORITY_SCHEMA_VERSION = "SCOPE_DELTA_AUTHORITY_V1"
SCOPE_DELTA_AUTHORITY_EVIDENCE_SCHEMA_VERSION = "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1"
CONTRACT_PATCH_PLAN_SCHEMA_VERSION = "CONTRACT_PATCH_PLAN_V1"

AUTHORITY_CATEGORY_AI_INFERRED = "ai_inferred"
AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE = "human_review_directive"
AUTHORITY_CATEGORY_EXISTING_PARENT_CONTRACT = "existing_parent_contract"
AUTHORITY_CATEGORY_RELATED_ISSUE_DEPENDENCY = "related_issue_dependency"

DIRECTIVE_CONFIDENCE_EXPLICIT = "explicit"
DIRECTIVE_CONFIDENCE_AMBIGUOUS = "ambiguous"
DIRECTIVE_CONFIDENCE_CONFLICTING = "conflicting"
DIRECTIVE_CONFIDENCE_INFERRED = "inferred"

SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED = "contract_update_required"
SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION = "human_escalation"
SCOPE_DELTA_AUTHORITY_ROUTE_NOT_TRIGGERED = "not_triggered"

REASON_EXPLICIT_HUMAN_CONTRACT_DIRECTIVE = "explicit_human_contract_directive"
REASON_AMBIGUOUS_HUMAN_DIRECTIVE = "ambiguous_human_directive"
REASON_CONFLICTING_HUMAN_DIRECTIVES = "conflicting_human_directives"
REASON_REQUIRES_ISSUE_SPLIT = "requires_issue_split"
REASON_EXPANDS_ALLOWED_PATHS = "expands_allowed_paths"
REASON_CHANGES_PERMISSION_BOUNDARY = "changes_permission_boundary"
REASON_CHANGES_EXTERNAL_SERVICE_BOUNDARY = "changes_external_service_boundary"
REASON_DESTRUCTIVE_OR_NON_IDEMPOTENT_OPERATION = "destructive_or_non_idempotent_operation"
REASON_AI_INFERRED_SCOPE_DELTA = "ai_inferred_scope_delta"
REASON_UNTRUSTED_AUTHOR_ASSOCIATION = "untrusted_author_association"
REASON_MISSING_BASE_ISSUE_BODY_SHA256 = "missing_base_issue_body_sha256"
REASON_CROSS_TARGET_DIRECT_MUTATION = "cross_target_direct_mutation"

NEXT_STEP_RERUN_REFINEMENT_AFTER_CONTRACT_UPDATE = "rerun_refinement_after_contract_update"

SCOPE_DELTA_AUTHORITY_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_BOUNDARY_FLAG_KEYS = (
    "expands_allowed_paths",
    "changes_permission_boundary",
    "changes_external_service_boundary",
    "destructive_or_non_idempotent_operation",
    "requires_issue_split",
)

_BOUNDARY_KEYWORD_PATTERNS = {
    "changes_permission_boundary": re.compile(
        r"(permission (?:escalat|boundary|delta)|権限(?:昇格|境界|差分)|"
        r"privilege escalat|grant.*(sudo|root)|sudo access|root access)",
        re.IGNORECASE,
    ),
    "changes_external_service_boundary": re.compile(
        r"((?<!no paid )external service|外部サービス|third[- ]party api|external api|call.*external)",
        re.IGNORECASE,
    ),
    "destructive_or_non_idempotent_operation": re.compile(
        r"((?<!non[- ])\bdestructive\b|\birreversible\b|\bnon[- ]idempotent\b|"
        r"破壊的(?!でない)|force[- ]push|rm -rf|drop table)",
        re.IGNORECASE,
    ),
    "requires_issue_split": re.compile(
        # A role/component/class split is not an Issue partition. Only text
        # that unambiguously requires multiple/separate Issues (or explicitly
        # states an Issue partition) may enter this fail-closed lane.
        r"(split into (multiple|separate) issues?|"
        r"(?:multiple|separate) issues?(?:\s*(?:and|/)?\s*PRs?)?|"
        r"issue partition|別[Ii]ssue.*分割|複数.*[Ii]ssue.*分割)",
        re.IGNORECASE,
    ),
}

_ALLOWED_PATHS_EXPANSION_PHRASE_RE = re.compile(
    r"(add|adding|added|expand|expanded|拡張|追加|必要|necessary).{0,30}"
    r"(allowed paths?|allowed path|許可(?:された)?パス)",
    re.IGNORECASE,
)

_ALLOWED_PATHS_DIRECTIVE_RE = re.compile(
    r"(allowed paths|allowed path|許可(?:された)?パス)",
    re.IGNORECASE,
)

_DIRECTIVE_NEGATION_PATTERNS = (
    re.compile(r"least privilege.{0,25}(?:ではない|not)", re.IGNORECASE),
    re.compile(r"least privilege.{0,25}not(?:\s+necessary|[\s-]needed)", re.IGNORECASE),
    re.compile(r"no secrets?.{0,20}not(?:\s+guaranteed|確約され)", re.IGNORECASE),
    re.compile(r"possible\s+(?:service|paid service|external service)", re.IGNORECASE),
    re.compile(r"not\s+(?:guaranteed|assured|guarantee)", re.IGNORECASE),
)

_NON_QUOTED_GROUP = (
    r"`[^`]*`",
    r'"[^"]*"',
    r"'[^']*'",
)


_URL_START_RE = re.compile(r"(?<![\w/])(?:https?://|www\.)", re.IGNORECASE)
_LINK_OPEN_RE = re.compile(r"\]\(\s*")
_REFERENCE_DESTINATION_RE = re.compile(r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?:\n[ \t]{0,3})?")
_ANGLE_PATH_DESTINATION_RE = re.compile(r"<(?:(?:\.claude|docs|src|scripts|tests|\.github)/)[^>\n]+>")


def _without_link_destinations(text: str) -> str:
    """Mask source spans, not candidate substrings, before matching paths.

    A URL's `/docs/...` is a valid-looking suffix even though the *whole*
    URL is not a repository path. Keep the original character coordinates and
    surrounding prose so adjacent real literals remain independently visible.
    This is a bounded destination pre-pass, not a Markdown document parser.
    """
    spans: list[tuple[int, int]] = []
    for match in _LINK_OPEN_RE.finditer(text):
        start = match.end()
        if start < len(text) and text[start] == "<":
            end = text.find(">", start + 1)
            if end != -1 and "\n" not in text[start:end]:
                spans.append((start, end + 1))
            continue
        depth = 0
        end = start
        while end < len(text) and text[end] not in "\n\r":
            ch = text[end]
            if ch == "\\" and end + 1 < len(text) and text[end + 1] not in "\n\r":
                # CommonMark backslash-escaped punctuation is literal URL
                # content, not a nesting delimiter (notably \\) and \\().
                end += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            end += 1
        if end < len(text) and text[end] == ")":
            spans.append((start, end))
    for match in _REFERENCE_DESTINATION_RE.finditer(text):
        start = match.end()
        if start < len(text) and text[start] == "<":
            end = text.find(">", start + 1)
            if end != -1 and "\n" not in text[start:end]:
                spans.append((start, end + 1))
        else:
            end = start
            while end < len(text) and not text[end].isspace():
                end += 1
            spans.append((start, end))
    spans.extend(match.span() for match in _ANGLE_PATH_DESTINATION_RE.finditer(text))
    for match in _URL_START_RE.finditer(text):
        # Explicit backtick-quoted URLs are unsafe literal *candidates*, not
        # link destinations. Preserve the pre-existing mixed-literal veto.
        if text[:match.start()].count("`") % 2:
            continue
        end = match.end()
        depth = 0
        while end < len(text) and not text[end].isspace() and text[end] not in "<>`":
            if text[end] == "\\" and end + 1 < len(text) and text[end + 1] not in "\n\r":
                end += 2
                continue
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                if depth == 0:
                    break
                depth -= 1
            end += 1
        spans.append((match.start(), end))
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _extract_path_literals_from_text(text: str) -> list[str]:
    """Extract only safe repository-relative Allowed Paths literals.

    A directive may mention a path while denying it (or quote an unsafe URL),
    but neither is an exact positive scope delta.  Keep this parser narrower
    than the general path-token extractor because its output can lift the
    Allowed Paths boundary from human escalation to contract-update-only.
    """
    if _allowed_paths_expansion_is_negated(text):
        return []
    path_literals: list[str] = []
    for match in PATH_TOKEN_RE.finditer(_without_link_destinations(text or "")):
        candidate = match.group("path") or match.group("bare") or ""
        normalized = _normalize_exact_repository_path_literal(candidate)
        if normalized is None:
            # A mixed directive is not an exact positive scope delta.  In
            # particular, accepting a safe literal while silently dropping a
            # URL, absolute path, traversal, or control-byte token would let
            # the unnormalized directive reach the contract-patch consumer.
            if _is_unsafe_path_literal(candidate):
                return []
            continue
        if normalized not in path_literals:
            path_literals.append(normalized)
    return path_literals


def _allowed_paths_expansion_is_negated(text: str) -> bool:
    """Reject directives that explicitly deny the candidate expansion."""
    return bool(
        re.search(
            r"(?:do\s+not|don't|never|must\s+not|禁止|追加しない|拡張しない|含めない).{0,120}"
            r"(?:allowed\s+paths?|allowed\s+path|許可(?:された)?パス)",
            text or "",
            re.IGNORECASE,
        )
        or re.search(
            r"(?:allowed\s+paths?|allowed\s+path|許可(?:された)?パス).{0,120}"
            r"(?:追加しない|拡張しない|含めない|禁止)",
            text or "",
            re.IGNORECASE,
        )
    )


def _normalize_exact_repository_path_literal(candidate: str) -> str | None:
    """Return one normalized repository-relative literal or ``None``.

    The existing general token grammar intentionally also sees URLs and other
    prose-adjacent values.  Those must not become authorization-bearing exact
    path deltas.  Only forward-slash repository-relative literals without
    traversal/control bytes are eligible.
    """
    if not isinstance(candidate, str) or not candidate:
        return None
    if "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        return None
    if "\\" in candidate or "://" in candidate or candidate.startswith("/"):
        return None
    # #2333 AC4: an exact Allowed Paths literal must never carry an HTML/CF_HTML
    # marker token. `<`/`>` reject any stray HTML tag or entity boundary, and
    # the explicit CF_HTML marker check is a defense-in-depth guard should the
    # marker ever appear pre-normalized without a plain `<` (e.g. through an
    # upstream partial decode).
    if "<" in candidate or ">" in candidate or "<!--StartFragment" in candidate:
        return None
    normalized = _normalize_path(candidate)
    if not normalized or "/" not in normalized or normalized.startswith("/"):
        return None
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return normalized


def _is_unsafe_path_literal(candidate: str) -> bool:
    """Return whether a rejected token is an unsafe path-like literal.

    Non-path backticked prose does not make a directive unsafe by itself, but
    a token that can name an external or non-repository target must make the
    entire Allowed Paths directive ineligible for automatic materialization.
    """
    if not isinstance(candidate, str) or not candidate:
        return False
    if "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        return True
    if "\\" in candidate or "://" in candidate or candidate.startswith("/"):
        return True
    # #2333 AC4: an HTML/CF_HTML-tagged token (e.g. an injected <script> tag
    # riding along a legitimate-looking path segment) must make the whole
    # mixed directive unsafe, not merely fail to normalize into a silently
    # dropped literal.
    if "<" in candidate or ">" in candidate or "<!--StartFragment" in candidate:
        return True
    return any(part in {"", ".", ".."} for part in candidate.split("/"))


def _strip_quoted_fragments(text: str) -> str:
    stripped = text
    for pattern in _NON_QUOTED_GROUP:
        stripped = re.sub(pattern, " ", stripped)
    return stripped

_DIRECTIVE_SECTION_MARKERS = (
    "revised acceptance criteria",
    "revised ac",
    "stop condition",
    "precondition",
    "前提条件",
    "allowed paths",
    "allowed paths expansion",
    "verification command",
    "contract update",
    "scope delta",
    "permission boundary",
    "全面改訂",
    "分割しない",
    "1 issue = 1 pr",
)

# #2812 fix_delta: originally only matched unordered `-`/`*` bullet
# markers. Extended to ALSO match Markdown ordered-list markers (`1. `,
# `2. `, `1)`, `2)` etc.) so a structured `human_review_directive`
# expressed as a native Markdown ordered list (Issue #2805 repro) is
# detected the same way an unordered bullet already was.
#
# A CF_HTML clipboard-paste `<ol><li>` envelope (Issue #2730 repro) is
# handled too -- but NOT by this pattern parsing or converting the raw
# `<ol><li>` HTML into Markdown. `_canonicalize_cf_html_envelope()`
# never touches the HTML fragment's content at all: it only locates the
# envelope boundary (StartFragment/EndFragment) and SELECTS the trailing
# duplicated Markdown-only rendition that the SAME clipboard payload
# already carries after that boundary (see that function's docstring).
# This pattern only ever sees that already-Markdown trailing text, the
# same as any other native Markdown ordered list. This is a minimal
# regex extension only -- no general Markdown/HTML parser is introduced
# (#2778 research parent, out-of-scope raw HTML tag detection).
#
# #2814 fix_delta (OWNER REQUEST_CHANGES finding A/B): the marker's
# surrounding whitespace is deliberately LINE-LOCAL (`[ \t]`, never bare
# `\s`, which also matches `\n`) so a marker-only line (e.g. a lone
# `1.` with the directive prose on the FOLLOWING line) can never absorb
# that next line's prose into the same match by consuming the newline
# between them -- that previously let `has_bullets` become `True` while
# `extract_directive_items()` (which processes one already-`splitlines()`
# line at a time and therefore never saw that merged span) still
# extracted zero items from the same input, a detector/extractor
# semantic split that could feed a false `explicit` classification in
# `classify_directive_confidence()`. The ordered-marker digit class is
# `_ORDERED_MARKER_RE_FRAGMENT` (ASCII `[0-9]` only, max 9 digits) --
# shared with `_ORDERED_LIST_ITEM_PREFIX_RE` below so the detector and
# the extractor can never drift onto different marker grammars again.
_ORDERED_MARKER_RE_FRAGMENT = r"[0-9]{1,9}[.)]"
_BULLET_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*]|" + _ORDERED_MARKER_RE_FRAGMENT + r")[ \t]+\S.*$",
    re.MULTILINE,
)

# #2812 fix_delta: matches the ordered-list marker prefix of a single
# (already `.strip()`-ped) line, e.g. "1. " / "2) " -- used by
# `extract_directive_items()` to extract content symmetrically with the
# existing unordered `"- "` / `"* "` prefix check below.
#
# #2814 fix_delta (finding B): shares `_ORDERED_MARKER_RE_FRAGMENT` with
# `_BULLET_LINE_RE` above (ASCII digits only, max 9 -- GFM ordered-list
# marker grammar) instead of a separately-maintained `\d+[.)]`, so a
# 10+ digit run, an Arabic-Indic digit (e.g. `١.`), or a full-width
# digit (e.g. `１.`) is never accepted as an ordered-list marker by
# either the detector or the extractor.
_ORDERED_LIST_ITEM_PREFIX_RE = re.compile(
    r"^" + _ORDERED_MARKER_RE_FRAGMENT + r"[ \t]+"
)

# #2086 AC1 P1 fix_delta: a bullet line's mere PRESENCE is not itself
# evidence of a scope-expansion directive (an observation note, a TODO
# item, or a pasted failure-log line can be a bullet too). This pattern
# requires the bullet's own CONTENT to carry an imperative
# directive-request verb -- Japanese te-kudasai / beki-da forms, or a
# common English imperative -- distinct from origin-lane assertion (which
# `operator_asserted_human_context` already carries independently).
_SEMANTIC_DIRECTIVE_VERB_RE = re.compile(
    r"(?:"
    r"して(?:ください|下さい|欲しい|ほしい)|"
    r"する(?:必要がある|べきです|べきだ|こと)|"
    r"を(?:修正|直|追加|拡張|対応|実装|変更|適用|統一|削除|除去)(?:して|する)|"
    r"\bfix(?:es|ed)?\b|\bupdate[sd]?\b|\badd(?:s|ed)?\b|\baddress(?:es|ed)?\b|"
    r"\bextend(?:s|ed)?\b|\bapply\b|\bapplied\b|\bchange[sd]?\b|\bmodify\b|"
    r"\bmodified\b|\brefactor\b|\bimplement(?:s|ed)?\b|\bremove[sd]?\b|"
    r"\bcorrect(?:s|ed)?\b|\bresolve[sd]?\b|\badjust(?:s|ed)?\b|\bmust\b|\bshould\b"
    r")",
    re.IGNORECASE,
)


# #2086 P1 fix_delta (iteration 3, OWNER REQUEST_CHANGES Blocker 4):
# `_SEMANTIC_DIRECTIVE_VERB_RE` is a lexical match -- it fires on a bare verb
# token anywhere in a bullet, with no regard for negation or tense/aspect.
# That means a PAST-TENSE STATUS statement ("This was fixed yesterday.") or
# a NEGATED imperative ("We should not expand Allowed Paths.") would also be
# promoted to `explicit`, even though neither is actually instructing a
# scope expansion. This is the authority classifier's entry point (Design
# Invariant #2: semantic authority must come from a genuine human
# directive), so it must stay narrow. `_NEGATION_OR_STATUS_MARKER_RE` is
# checked in a bounded window around each verb match; a hit anywhere in that
# window (negation marker before/after the verb, or a perfect/passive-status
# auxiliary before it) suppresses that match without disqualifying other,
# unrelated verb matches later in the same bullet.
_NEGATION_OR_STATUS_MARKER_RE = re.compile(
    r"(?:"
    r"\bnot\b|\bnever\b|n't\b|\bno\b|\bdon't\b|\bdo not\b|"
    r"\bdoesn't\b|\bdoes not\b|\bwon't\b|\bwill not\b|"
    r"\bwas\b|\bwere\b|\bhas been\b|\bhave been\b|\bhad been\b|"
    r"\balready\b|\bpreviously\b|\byesterday\b|"
    r"不要|しない|する必要はない|しなくてよい|しなくていい|"
    r"済み|済んだ|済んでいる|完了済み|修正済み|対応済み|"
    r"すでに|既に|昨日|先日"
    r")",
    re.IGNORECASE,
)


def _has_semantic_directive_bullet(text: "str | None") -> bool:
    """#2086 AC1 P1 fix_delta: does any bullet-list line in `text` carry an
    imperative directive-request verb (see `_SEMANTIC_DIRECTIVE_VERB_RE`)?
    Used only by the operator-selected human-context, no-known-marker
    branch of `classify_directive_confidence()` -- semantic content
    detection is kept separate from origin-lane assertion so that an
    observation/TODO/failure-log bullet list is never conflated with an
    actual scope-expansion directive just because the comment arrived on
    the trusted human-context lane.

    #2086 P1 fix_delta (iteration 3, Blocker 4): a verb match is only
    accepted as a genuine imperative/obligation directive when no negation
    or past-tense/perfect-status marker is present in a bounded window
    around it (`_NEGATION_OR_STATUS_MARKER_RE`) -- see that constant's
    docstring for the failure modes this closes (negated imperative,
    past-tense status report).
    """
    for item in extract_directive_items(text):
        for match in _SEMANTIC_DIRECTIVE_VERB_RE.finditer(item):
            window = item[max(0, match.start() - 25) : min(len(item), match.end() + 20)]
            if _NEGATION_OR_STATUS_MARKER_RE.search(window):
                continue
            return True
    return False
_CONCRETE_PERMISSION_DELTA_RE = re.compile(
    r"(?:permission[ _-]?(?:delta|profile)|permissions?[ _-]?profile|権限(?:差分|プロファイル))"
    r"\s*[:=]?\s*(?P<before>[^\n→]{1,120}?)\s*(?:->|→)\s*(?P<after>[^\n]{1,120})",
    re.IGNORECASE,
)
_CONCRETE_REPOSITORY_PATH_RE = re.compile(
    r"`(?P<path>(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^`\s]+/[^`\s]+)`"
)
_UNRELATED_PERMISSION_WIDENING_RE = re.compile(
    r"(?<!no )unrelated(?: privilege)? widening|"
    r"(?<!no )unrelated privilege widening|"
    r"無関係な権限拡大(?!なし)",
    re.IGNORECASE,
)

_ISSUE_COMMENT_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/issues/(?P<issue_number>\d+)#issuecomment-(?P<comment_id>\d+)$"
)
_PR_URL_FRAGMENT_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#")


def detect_boundary_flags(text: "str | None", *, expands_allowed_paths: bool = False) -> dict:
    """AC18: deterministic keyword-based boundary-flag detection.

    `text` is scanned for keywords indicating permission escalation, external
    service usage, destructive/non-idempotent operations, or a need to split
    into a separate Issue. `expands_allowed_paths` is supplied by the caller
    (derived from an actual Allowed Paths diff, not text matching).

    #2333 fix_delta (OWNER REQUEST_CHANGES P0-1): `text` is canonicalized via
    `_cf_html_canonical_text()` before scanning so a CF_HTML/clipboard-
    contaminated comment body is scanned on the SAME canonical view as
    `extract_directive_markers()` / `extract_directive_items()` /
    `classify_directive_confidence()` -- never the raw HTML+Markdown
    envelope. See `_cf_html_canonical_text()` for why this must be the one
    and only place each of those functions derives its working text from.
    """
    haystack = _cf_html_canonical_text(text)
    flags = {name: bool(pattern.search(haystack)) for name, pattern in _BOUNDARY_KEYWORD_PATTERNS.items()}
    flags["expands_allowed_paths"] = bool(
        expands_allowed_paths
        or _ALLOWED_PATHS_EXPANSION_PHRASE_RE.search(haystack)
        or _ALLOWED_PATHS_DIRECTIVE_RE.search(haystack)
    )
    return {key: flags.get(key, False) for key in _BOUNDARY_FLAG_KEYS}


def _has_explicit_exact_allowed_path_expansion(evidence: dict) -> bool:
    directives = evidence.get("extracted_directives") or []
    for item in directives:
        if not isinstance(item, str):
            continue
        if not _ALLOWED_PATHS_DIRECTIVE_RE.search(item):
            continue
        if _extract_path_literals_from_text(item):
            return True
    return False


# #2994: severity grammar. A heading's TEXT (Markdown: everything after the
# leading ``#``s; HTML: the concatenated inline text of an ``h1``-``h6``) must
# START with an optional number prefix (``1.`` / ``2)``) followed by either a
# bracketed severity ``[P<n>]`` (tag ``P<n>``) or the pre-existing
# ``P<n>-<n>`` form (tag ``P<n>-<n>``). A severity token embedded later in a
# heading (``### Notes about [P1] policy``) or in ordinary prose is NOT a
# finding heading.
# #3016: the heading-start grammar additionally accepts ``P<n>`` immediately
# followed by an ASCII ``:`` or a fullwidth ``：`` (tag ``P<n>``), e.g.
# ``### 1. P2：...`` / ``<h3>P2: ...</h3>``. The colon must directly follow the
# number, and the token must still START the heading text.
_SEVERITY_HEADING_TEXT_RE = re.compile(
    r"^\s*(?:[0-9]+[.)]\s*)?"
    r"(?:\[(P[0-9]+)\]|(P[0-9]+-[0-9]+)\b|(P[0-9]+)[:\uFF1A])",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_LINE_RE = re.compile(r"^\s*#+\s*(.*)$")


def extract_directive_markers(text: "str | None") -> list:
    """Detect known directive section markers (Revised AC, Stop Condition, ...).

    #2296 fix_delta iteration 6 (OWNER adversarial review, P1-4): severity-
    tagged headings (``## P0-1`` etc.) are NO LONGER merged into this
    function's return value. A bare severity-tagged heading (e.g. a
    heading literally named ``## P0-1`` followed by unrelated bullet text)
    must never, by itself, be treated as a scope-authoritative directive
    marker -- that conflated a severity LABEL with an actual scope-change
    DIRECTIVE, and caused ``classify_directive_confidence()`` to
    misclassify such headings as ``explicit`` via the generic
    marker-present code path. Severity-tag extraction now lives in the
    separate ``extract_severity_tags()`` function below; this function
    only ever detects the pre-existing fixed ``_DIRECTIVE_SECTION_MARKERS``
    set.

    #2333 fix_delta (OWNER REQUEST_CHANGES P0-1): `text` is canonicalized
    via `_cf_html_canonical_text()` before scanning, so a CF_HTML/clipboard-
    contaminated comment body is scanned on the SAME canonical view as
    `extract_directive_items()` / `detect_boundary_flags()` /
    `classify_directive_confidence()` -- never the raw HTML+Markdown
    envelope.
    """
    lowered = _cf_html_canonical_text(text).lower()
    markers = {marker for marker in _DIRECTIVE_SECTION_MARKERS if marker in lowered}
    return sorted(markers)


def _severity_tag_from_heading_text(heading_text: str) -> "str | None":
    match = _SEVERITY_HEADING_TEXT_RE.match(heading_text)
    if match is None:
        return None
    return (match.group(1) or match.group(2) or match.group(3)).upper()


def _find_html_comment_opener(line: str, start: int = 0) -> int:
    """Index of the first real ``<!--`` at/after ``start`` or -1 (#2994).

    A ``<!--`` is NOT an opener when it sits inside a closed same-line code span
    (a backtick run closed by a run of the exact same length) or when its ``<``
    is backslash-escaped. An unmatched backtick run is literal text.
    """
    i = start
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            run_end = i
            while run_end < n and line[run_end] == "`":
                run_end += 1
            run = run_end - i
            j = run_end
            closed_at = -1
            while j < n:
                if line[j] == "`":
                    k = j
                    while k < n and line[k] == "`":
                        k += 1
                    if k - j == run:
                        closed_at = k
                        break
                    j = k
                else:
                    j += 1
            i = closed_at if closed_at != -1 else run_end
            continue
        if line.startswith("<!--", i):
            return i
        i += 1
    return -1


def _scan_markdown_severity_tags(text: str) -> "set[str]":
    """Line-oriented Markdown scanner for severity headings (#2994).

    Skips (a) fenced code blocks -- GFM basic opener rule only: 3+ backticks or
    tildes at 0-3 spaces of indent, a closer of the same character and at least
    the opener length, an unclosed fence runs to EOF, and 4-space-indented or
    container-prefixed (``> ``) openers are NOT recognized -- and (b) HTML
    comments (``<!--`` .. first ``-->``, an unclosed comment runs to EOF). A
    ``<!--`` inside a fence is not a comment opener and a fence opener inside a
    comment is ignored (comment detection happens outside fences only). Text
    following a closing ``-->`` on the same line is still examined. A backtick
    "fence" whose info string contains a backtick is an inline code span, not an
    opener, and a ``<!--`` inside a closed code span or after a backslash is
    literal text, not a comment opener.
    """
    tags: "set[str]" = set()
    in_fence = False
    fence_char = ""
    fence_len = 0
    in_comment = False
    for line in text.splitlines():
        if in_fence:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
            continue
        if in_comment:
            close = line.find("-->")
            if close == -1:
                continue
            in_comment = False
            line = line[close + 3 :]
        else:
            opener = _parse_fence_opener(line)
            # GFM 4.5: a backtick fence's info string may not contain a
            # backtick (that is an inline code span, not a fence opener).
            if opener is not None and not (
                opener[0] == "`" and "`" in line.lstrip(" ")[opener[1] :]
            ):
                fence_char, fence_len = opener
                in_fence = True
                continue
        # Remove every HTML comment span from the visible part of the line.
        visible: "list[str]" = []
        rest = line
        while True:
            open_at = _find_html_comment_opener(rest)
            if open_at == -1:
                visible.append(rest)
                break
            visible.append(rest[:open_at])
            close = rest.find("-->", open_at + 4)
            if close == -1:
                in_comment = True
                break
            rest = rest[close + 3 :]
        heading = _MARKDOWN_HEADING_LINE_RE.match("".join(visible))
        if heading is None:
            continue
        tag = _severity_tag_from_heading_text(heading.group(1))
        if tag is not None:
            tags.add(tag)
    return tags


class _HeadingTextCollector(HTMLParser):
    """Collect the inline text of ``h1``-``h6`` (entity-decoded). Standard
    library only; deliberately NOT a general HTML parser/validator (#2994):
    headings inside ``pre`` / ``script`` / ``style`` are ignored and comments
    are not data."""

    _HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _OPAQUE_TAGS = frozenset({"pre", "script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: "list[str]" = []
        self._current: "list[str] | None" = None
        self._opaque_depth = 0

    def _finish_heading(self) -> None:
        if self._current is not None:
            self.headings.append(" ".join("".join(self._current).split()))
            self._current = None

    def handle_starttag(self, tag, attrs):
        if tag in self._OPAQUE_TAGS:
            self._opaque_depth += 1
        elif tag in self._HEADING_TAGS and self._opaque_depth == 0:
            self._finish_heading()
            self._current = []

    def handle_endtag(self, tag):
        if tag in self._OPAQUE_TAGS:
            self._opaque_depth = max(0, self._opaque_depth - 1)
        elif tag in self._HEADING_TAGS:
            self._finish_heading()

    def handle_data(self, data):
        if self._current is not None and self._opaque_depth == 0:
            self._current.append(data)

    def finish(self) -> "list[str]":
        self.close()
        self._finish_heading()
        return self.headings


def _html_heading_texts(fragment: str) -> "list[str]":
    collector = _HeadingTextCollector()
    collector.feed(fragment)
    return collector.finish()


def extract_severity_tags(text: "str | None") -> list:
    """Detect owner adversarial-review severity-tagged headings and return the
    tags (uppercased, de-duplicated, sorted): ``P0-1`` (``## P0-1`` /
    ``### P1-3``), bracketed ``P1`` (``### [P1]`` / ``### 1. [P1] ...``) or
    colon ``P2`` (``### 1. P2：...`` / ``### P2: ...``, #3016).
    Grammar: see ``_SEVERITY_HEADING_TEXT_RE`` (heading text must START with an
    optional ``\\d+[.)]`` number prefix and then ``[P<n>]``, ``P<n>-<n>`` or
    ``P<n>`` directly followed by ASCII ``:`` / fullwidth ``：``; case
    insensitive).

    #2994 scan regions:

    - Complete CF_HTML envelope (outer ``html``/``body`` immediately followed
      by ``<!--StartFragment-->`` and a later FIRST ``<!--EndFragment-->``,
      i.e. the envelope shape ``_canonicalize_cf_html_envelope()`` recognizes):
      HTML headings (``h1``-``h6``) are extracted ONLY from the interior
      Start..first-End region via ``HTMLParser`` (a Markdown tail is not
      required -- HTML-only fragments are valid), and the Markdown scanner runs
      ONLY on the tail after the first EndFragment (wrapper close tags
      stripped), never on the envelope interior.
    - Incomplete envelope (open shape matched but no EndFragment): the HTML
      region cannot be bounded, so HTML heading extraction is skipped; the
      ordinary Markdown scanner still runs on the whole body (fence / comment
      exclusions apply). Callers that need such a comment treated as a trigger
      use ``user_requested``.
    - No envelope: the whole body goes to the Markdown scanner. Raw HTML
      headings outside an envelope are not severity findings.

    Fenced code blocks and HTML comments are excluded from the Markdown scan
    (``_scan_markdown_severity_tags``).

    #2296 fix_delta iteration 6 (P1-4): kept as an independent function
    from ``extract_directive_markers()`` so that severity-tag detection never
    feeds ``classify_directive_confidence()`` / contract_patch_operations via
    that function's marker-present code path. #2994 does not change
    ``extract_directive_markers()`` / ``_canonicalize_cf_html_envelope()`` /
    ``_cf_html_canonical_text()``; the envelope constants are only READ here.
    Consumers that need the ``severity_tagged_anchor_findings`` signal for
    ``semantic_review_trigger.py`` (Step 2.5, #2296) call this function
    directly (see ``build_semantic_review_trigger_input()``); this module
    itself remains unaware of that downstream caller's contract
    (responsibility separation preserved).
    """
    body = text or ""
    tags: "set[str]" = set()
    markdown_text = body
    open_match = _CF_HTML_ENVELOPE_OPEN_RE.match(body)
    if open_match is not None:
        end_match = _CF_HTML_END_FRAGMENT_RE.search(body, open_match.end())
        if end_match is not None:
            for heading_text in _html_heading_texts(body[open_match.end() : end_match.start()]):
                tag = _severity_tag_from_heading_text(heading_text)
                if tag is not None:
                    tags.add(tag)
            tail = body[end_match.end() :]
            wrapper_close = _CF_HTML_WRAPPER_CLOSE_RE.match(tail)
            markdown_text = tail[wrapper_close.end() :] if wrapper_close else tail
    tags |= _scan_markdown_severity_tags(markdown_text)
    return sorted(tags)


# #2333: CF_HTML/clipboard envelope boundary. A CF_HTML clipboard paste
# (produced by rich-text editors, see #2290) wraps a fragment of HTML between
# ``<!--StartFragment-->``/``<!--EndFragment-->`` markers. When such a paste
# lands in a GitHub anchor comment unmodified, the literal HTML precedes a
# trailing Markdown rendition of the *same* content. This is intentionally a
# narrow, deterministic marker-driven boundary scoped to the OBSERVED
# #2290/#40/#77 artifact shape -- not a general Microsoft CF_HTML clipboard
# format parser (that broader format allows arbitrary surrounding context,
# multiple fragment pairs, etc. -- all out of scope here) -- so legitimate
# GFM raw HTML (``<details>``/``<summary>`` etc., already used in Issue
# bodies) that never carries these CF_HTML markers is left untouched.
_CF_HTML_START_FRAGMENT_RE = re.compile(r"<!--\s*StartFragment\s*-->", re.IGNORECASE)
_CF_HTML_END_FRAGMENT_RE = re.compile(r"<!--\s*EndFragment\s*-->", re.IGNORECASE)
# #2333 fix_delta (OWNER REQUEST_CHANGES P0-2): the observed #2290/#40/#77
# envelope always OPENS at the very start of the comment body with an outer
# ``<html>``/``<body>`` wrapper immediately followed by the StartFragment
# marker. Anchoring on this exact opening shape (rather than merely
# searching for a StartFragment marker anywhere in the text) is what lets an
# ordinary Markdown/prose mention of the marker strings -- including a
# trailing Markdown tail that itself *explains* this exact technique in
# backticks (the #2333 OWNER review comment that reported this bug is
# itself such a case) -- pass through unmodified instead of being
# misidentified as contamination.
_CF_HTML_ENVELOPE_OPEN_RE = re.compile(
    r"^\s*<html[^>]*>\s*<body[^>]*>\s*<!--\s*StartFragment\s*-->",
    re.IGNORECASE | re.DOTALL,
)
# Outer wrapper close tags (``</body>``/``</html>``) that immediately follow
# the genuine terminating EndFragment marker in the observed envelope shape.
_CF_HTML_WRAPPER_CLOSE_RE = re.compile(
    r"^\s*(?:</body>\s*)?(?:</html>\s*)?", re.IGNORECASE
)


def _canonicalize_cf_html_envelope(text: "str | None") -> "str | None":
    """Canonicalize (or reject) a CF_HTML/clipboard-contaminated comment body.

    Returns ``text`` unchanged when the text does not open with the
    observed #2290/#40/#77 envelope shape -- an outer ``<html>``/``<body>``
    wrapper immediately followed by a ``<!--StartFragment-->`` marker at the
    very START of the comment body (see ``_CF_HTML_ENVELOPE_OPEN_RE``). This
    covers both the overwhelming majority of anchor comments that carry no
    CF_HTML marker at all, AND ordinary Markdown/prose that merely
    *mentions* the marker strings (e.g. in backticks, to explain this exact
    technique) without actually being wrapped in the envelope -- #2333
    fix_delta (OWNER REQUEST_CHANGES P0-2): a bare marker-string mention
    must never, by itself, invalidate an otherwise-clean directive.

    When the envelope-open shape IS matched, the text from the end of the
    FIRST ``<!--EndFragment-->`` marker that follows the opening
    StartFragment (never the *last* EndFragment anywhere in the string --
    a later, unrelated EndFragment mention further down in a duplicated
    Markdown tail must never be mistaken for the genuine terminator; #2333
    fix_delta OWNER REQUEST_CHANGES P0-2) -- with an immediately-following
    HTML wrapper close tag sequence such as ``</body></html>`` stripped --
    is taken as the canonical trailing Markdown-only content. This is the
    #2290/#40/#77-shaped envelope: HTML wrapper -> StartFragment -> HTML
    fragment -> EndFragment -> wrapper close -> duplicated Markdown.

    Returns None (meaning: the caller must treat the directive as
    disabled / not extractable) only when the envelope-open shape is
    matched but the boundary genuinely cannot be uniquely determined:

    - no ``<!--EndFragment-->`` marker follows the opening StartFragment at
      all, or
    - the trailing content (after stripping the wrapper-close tags) is
      itself empty/whitespace-only, or immediately begins with another
      nested/duplicate ``<!--StartFragment-->`` marker -- i.e. the
      Markdown-only tail cannot be uniquely identified.

    A mere mention of the marker strings, or of ``<html``/``<body`` text,
    ANYWHERE INSIDE an otherwise-valid canonical tail (for example, a
    regression-test fixture excerpt embedded later in the Markdown body)
    does NOT trigger rejection -- only genuine structural ambiguity at the
    tail's own boundary does.
    """
    if not text:
        return text
    open_match = _CF_HTML_ENVELOPE_OPEN_RE.match(text)
    if open_match is None:
        # Either no StartFragment marker anywhere, or one appears without
        # the observed envelope-open shape at the start of the body -- an
        # unrelated mention in ordinary Markdown/prose. Pass through
        # byte-for-byte unchanged.
        return text
    end_match = _CF_HTML_END_FRAGMENT_RE.search(text, open_match.end())
    if end_match is None:
        # A genuine envelope open with no terminator anywhere -- the
        # fragment boundary cannot be uniquely determined.
        return None
    trailing = text[end_match.end():]
    wrapper_close_match = _CF_HTML_WRAPPER_CLOSE_RE.match(trailing)
    if wrapper_close_match:
        trailing = trailing[wrapper_close_match.end():]
    if _CF_HTML_START_FRAGMENT_RE.match(trailing.lstrip()):
        # A nested/duplicate envelope immediately follows -- the
        # Markdown-only tail cannot be uniquely identified.
        return None
    canonical = trailing.strip()
    if not canonical:
        return None
    return canonical


def _cf_html_canonical_text(text: "str | None") -> str:
    """Canonicalize CF_HTML/clipboard contamination exactly once, so every
    semantic extractor (`extract_directive_markers`, `detect_boundary_flags`,
    `classify_directive_confidence`, `extract_directive_items`) observes the
    SAME canonical view of a comment body for the same raw input -- never a
    mix of the raw HTML+Markdown envelope and the canonicalized Markdown-only
    tail.

    #2333 fix_delta (OWNER REQUEST_CHANGES P0-1): the production call site
    (`_build_scope_delta_authority_evidence()` in `run_refinement_preflight.py`)
    calls each of those four functions independently with the SAME raw
    `comment_body`. Because `_canonicalize_cf_html_envelope()` is a pure,
    deterministic function of its input, having every one of those four
    functions canonicalize via this single helper -- rather than relying on
    the call site to canonicalize once and thread a shared value through --
    is behaviorally equivalent (same raw input -> same canonical output,
    every time) while keeping the boundary entirely inside this module.

    Returns `""` (not `None`) when canonicalization determines the directive
    is disabled/unextractable, so every caller can treat the return value as
    ordinary text without a separate `None` branch.
    """
    canonical = _canonicalize_cf_html_envelope(text)
    return canonical if canonical is not None else ""


def extract_directive_items(text: "str | None") -> list:
    """Extract bullet-list lines (candidate directive text) from a comment body.

    #2333: raw anchor comment bodies may contain a CF_HTML/clipboard envelope
    (``<!--StartFragment-->``/``<!--EndFragment-->`` markers wrapping HTML
    that duplicates a trailing Markdown rendition of the same content, see
    #2290). Such an envelope is canonicalized -- or the whole comment is
    treated as having no extractable directive -- via
    ``_canonicalize_cf_html_envelope()`` *before* bullet-line extraction, so
    contamination never reaches the bullet items this function returns (and
    therefore never reaches the downstream contract-patch operations built
    from them).

    #2812 fix_delta: a Markdown ORDERED-list marker line (``1. ``, ``2) ``,
    etc. -- see ``_ORDERED_LIST_ITEM_PREFIX_RE``) is now extracted
    symmetrically with the pre-existing unordered ``"- "``/``"* "`` prefix
    check below, so a structured directive expressed as a native ordered
    list (Issue #2805) or a CF_HTML clipboard-paste ordered list rendition
    (Issue #2730, already canonicalized above) is no longer silently
    dropped.
    """
    canonical = _canonicalize_cf_html_envelope(text)
    if canonical is None:
        return []
    items = []
    for line in (canonical or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            content = stripped[2:].strip()
            if content:
                items.append(content)
            continue
        ordered_match = _ORDERED_LIST_ITEM_PREFIX_RE.match(stripped)
        if ordered_match:
            content = stripped[ordered_match.end():].strip()
            if content:
                items.append(content)
    return items


def classify_directive_confidence(
    text: "str | None",
    markers: "list | None" = None,
    *,
    operator_asserted_human_context: bool = False,
) -> str:
    """Deterministic confidence classification for a single review comment.

    - no directive markers found -> inferred (the AI would have to infer intent)
    - markers found + at least one bullet-list line -> explicit
    - markers found but no structured bullet list -> ambiguous

    #2086 AC1/AC3: an ``operator_asserted_human_context`` directive (the
    comment URL was passed on the ``preflight.run.with_human_context`` /
    ``contract_update.run.with_human_context`` lane -- never inferred from
    comment metadata, see Design Invariant #1) that has at least one
    structured bullet-list line is treated as ``explicit`` even when it does
    not contain one of the fixed ``_DIRECTIVE_SECTION_MARKERS`` section
    headings. A hand-written ``ANCHOR_SCOPE_REFRAME_V1`` payload or a
    "Revised Acceptance Criteria" heading is not required for a trusted,
    operator-selected human-context directive to be recognized as an
    explicit scope-mutation directive -- only the ``with_agent_report`` /
    unlabeled lanes remain excluded from this relaxation (AC6, gated
    entirely by the caller-supplied ``operator_asserted_human_context`` flag,
    which is only ever ``True`` for the human-context lane -- see
    ``_resolve_scope_delta_source_kind()`` in ``run_refinement_preflight.py``).

    #2333 fix_delta (OWNER REQUEST_CHANGES P0-1): `text` is canonicalized
    via `_cf_html_canonical_text()` at entry, before the bullet check and
    before falling back to `extract_directive_markers(text)`, so this
    function's own view of the comment body is the SAME canonical view as
    `extract_directive_markers()` / `detect_boundary_flags()` /
    `extract_directive_items()` -- never a mix of the raw HTML+Markdown
    envelope and the canonicalized Markdown-only tail. A caller-supplied
    `markers` argument is trusted as-is (it is expected to already be the
    canonical-view output of `extract_directive_markers()`).
    """
    text = _cf_html_canonical_text(text)
    marker_list = markers if markers is not None else extract_directive_markers(text)
    has_bullets = bool(_BULLET_LINE_RE.search(text or ""))
    if not marker_list:
        # #2086 AC1 P1 fix_delta: origin-lane assertion
        # (`operator_asserted_human_context`) and semantic-directive
        # detection (`_has_semantic_directive_bullet`) are separate
        # concerns -- ANY bullet existing in a human-context comment is not
        # itself sufficient; the bullet content must actually carry an
        # imperative directive-request verb.
        if operator_asserted_human_context and _has_semantic_directive_bullet(text):
            return DIRECTIVE_CONFIDENCE_EXPLICIT
        return DIRECTIVE_CONFIDENCE_INFERRED
    if has_bullets:
        return DIRECTIVE_CONFIDENCE_EXPLICIT
    return DIRECTIVE_CONFIDENCE_AMBIGUOUS


def parse_issue_comment_url(url: "str | None") -> "dict | None":
    """AC16: structurally parse a GitHub issue-comment URL.

    Returns None for PR review comment/discussion URLs, malformed URLs, or
    non-issue-comment hosts/paths (fail-closed, no substring matching).
    """
    if not isinstance(url, str) or not url:
        return None
    if _PR_URL_FRAGMENT_RE.match(url):
        return None
    match = _ISSUE_COMMENT_URL_RE.match(url)
    if not match:
        return None
    return {
        "owner": match.group("owner"),
        "repo": match.group("repo"),
        "issue_number": int(match.group("issue_number")),
        "comment_id": match.group("comment_id"),
    }


def validate_scope_delta_authority_evidence_url(
    evidence: dict,
    *,
    target_issue_number=None,
    expected_repo=None,
) -> bool:
    """AC16: fail-closed structural URL validation for issue_comment /
    pull_request_review evidence.

    Non issue_comment/pull_request_review source_kind values are not subject
    to this check (parent_issue / related_issue / agent_inferred evidence do
    not carry an issue-comment URL to validate).

    PR #1332 review fix (P0/P1):
    - issue_comment evidence now requires `expected_repo` to be provided by
      the caller (classify_scope_delta_authority) so that a same-issue-number
      URL from a *different* repository is fail-closed rejected rather than
      silently accepted when the caller forgets to pass expected_repo.
    - pull_request_review evidence is not yet structurally verifiable against
      `pull_request_url` / `_links.pull_request` (SCOPE_DELTA_AUTHORITY_EVIDENCE_V1
      does not carry those fields today), so it is fail-closed rejected
      unconditionally until a follow-up adds that verification. A genuine
      issue-comment URL mislabeled as pull_request_review is still detected
      and rejected explicitly for a clearer reason, but the net effect (not
      accepted) is unchanged.
    """
    source_kind = evidence.get("source_kind")
    if source_kind not in ("issue_comment", "pull_request_review"):
        return True
    if source_kind == "pull_request_review":
        # Fail-closed: pull_request_review evidence cannot yet be verified
        # against pull_request_url / _links.pull_request / repo / PR number,
        # so it is never accepted (AC16 hardening, PR #1332 review).
        return False

    if not expected_repo:
        # Fail-closed: repo/owner cross-check is mandatory for issue_comment
        # evidence. Without expected_repo we cannot rule out a same-issue-
        # number URL pointed at a different repository.
        return False

    parsed = parse_issue_comment_url(evidence.get("comment_url"))
    if parsed is None:
        return False
    expected_parts = expected_repo.lower().split("/", 1)
    if [parsed["owner"].lower(), parsed["repo"].lower()] != expected_parts:
        return False
    if target_issue_number is not None and parsed["issue_number"] != target_issue_number:
        return False
    issue_url = evidence.get("issue_url")
    if issue_url and target_issue_number is not None:
        expected_html = f"https://github.com/{expected_repo}/issues/{target_issue_number}"
        expected_api = f"https://api.github.com/repos/{expected_repo}/issues/{target_issue_number}"
        if issue_url.lower() not in (expected_html.lower(), expected_api.lower()):
            return False
    return True


class ContractPatchPlanBaseShaMissingError(ValueError):
    """Raised by build_contract_patch_plan_v1() when base_issue_body_sha256
    is missing (PR #1332 review fix, P1). CONTRACT_PATCH_PLAN_V1 is applied
    against a specific Issue body snapshot by issue-author/edit-issue; a null
    base sha256 would let a stale or mismatched Issue body be patched
    (wrong section, lost concurrent edits) with no way to detect it.
    """


def build_contract_patch_plan_v1(
    *,
    target_issue_number,
    base_issue_body_sha256,
    source_evidence: list,
    operations: list,
) -> dict:
    """AC3/AC19: build a CONTRACT_PATCH_PLAN_V1 payload.

    Generation-only: `forbidden` always lists direct_github_write and
    implementation_phase_transition so the plan can never be mistaken for an
    executed write. Callers (issue-author / edit-issue skill) apply the plan.

    PR #1332 review fix (P1): base_issue_body_sha256 is mandatory and
    non-null (contract_patch_plan_v1.schema.json now requires a non-empty
    string) -- callers must resolve a real Issue body sha256 before a
    contract_update_required route can be materialized into a patch plan.
    """
    if not base_issue_body_sha256:
        raise ContractPatchPlanBaseShaMissingError(
            "base_issue_body_sha256 is required (non-null, non-empty) to "
            "build a CONTRACT_PATCH_PLAN_V1 -- refusing to generate a patch "
            "plan against an unresolved Issue body snapshot"
        )
    return {
        "schema_version": CONTRACT_PATCH_PLAN_SCHEMA_VERSION,
        "target_issue_number": target_issue_number,
        "base_issue_body_sha256": base_issue_body_sha256,
        "source_evidence": source_evidence,
        "operations": operations,
        "forbidden": ["direct_github_write", "implementation_phase_transition"],
        "required_next_step": NEXT_STEP_RERUN_REFINEMENT_AFTER_CONTRACT_UPDATE,
    }


_MARKER_TO_CONTRACT_SECTION = {
    "revised acceptance criteria": "Acceptance Criteria",
    "revised ac": "Acceptance Criteria",
    "stop condition": "Stop Conditions",
    "precondition": "Acceptance Criteria",
    "前提条件": "Acceptance Criteria",
    "allowed paths": "Allowed Paths",
    "allowed paths expansion": "Allowed Paths",
    "verification command": "Verification Commands",
}


_COMMENT_SECTION_HEADINGS = {
    "revised acceptance criteria": "Acceptance Criteria",
    "revised ac": "Acceptance Criteria",
    "acceptance criteria": "Acceptance Criteria",
    "stop condition": "Stop Conditions",
    "stop conditions": "Stop Conditions",
    "verification command": "Verification Commands",
    "verification commands": "Verification Commands",
    "allowed paths": "Allowed Paths",
    "allowed paths expansion": "Allowed Paths",
    "in scope": "In Scope",
    "out of scope": "Out of Scope",
}
_AC_LINE_RE = re.compile(r"-\s+(?:\[[ xX]\]\s*)?AC[0-9]+\s*:\s*\S.*", re.IGNORECASE)
_STOP_DIRECTIVE_RE = re.compile(r"Stop Condition\s*を追加してください\s*:\s*(\S.*)", re.IGNORECASE)
_VC_DIRECTIVE_RE = re.compile(r"Verification Commands?\s*:\s*((?:-\s+|\$\s+)\S.*)", re.IGNORECASE)
# A single inline, backtick-quoted command is a structured VC item only when
# its entire payload is one bounded argv-like command (no shell metacharacters).
_VC_BACKTICK_DIRECTIVE_RE = re.compile(
    r"Verification Commands?\s*:\s*`(?P<command>(?:uv|pnpm|python3|node|git|rg)"
    r"(?:[ \t]+[A-Za-z0-9_./:=+@-]+)+)`",
    re.IGNORECASE,
)
_INLINE_SECTION_LABEL_RE = re.compile(r"(?:Stop Conditions?|Verification Commands?)\s*:", re.IGNORECASE)
_CONFLICTING_PATH_BULLET_LABEL_RE = re.compile(
    r"^(?:Stop Conditions?|Verification Commands?|Acceptance Criteria|Revised AC|In Scope|Out of Scope)\s*[:：]",
    re.IGNORECASE,
)
# Indentation is not section authority: a nested directive under a numbered AC
# must not be laundered into its continuation text by a pre-existing plan.
_CROSS_SECTION_CONTINUATION_RE = re.compile(
    r"(?:[-*]\s+)?(?:Allowed Paths?|Stop Conditions?|Verification Commands?|In Scope|Out of Scope)"
    r"\s*(?:[:：]|(?:を|に)\s*追加してください\s*[:：]?)",
    re.IGNORECASE,
)


def _structured_comment_items(body: str) -> list[tuple[str | None, str, str]]:
    """Associate the *actual* bullet with its H2 section without changing evidence keys.

    Only indented continuation lines of a bullet are retained. Unrelated
    paragraphs and bullets outside recognized sections do not become patches.
    """
    canonical = _canonicalize_cf_html_envelope(body)
    if canonical is None:
        return []
    items: list[tuple[str | None, str, str]] = []
    current: str | None = None
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in (canonical or "").splitlines():
        if in_fence:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
            continue
        opener = _parse_fence_opener(line)
        if opener:
            fence_char, fence_len = opener
            in_fence = True
            continue
        heading = _parse_heading(line)
        if heading is not None:
            current = _COMMENT_SECTION_HEADINGS.get(heading.lower())
            continue
        stripped = line.strip()
        bullet = _BULLET_LINE_RE.fullmatch(line)
        if bullet:
            content = stripped[2:].strip() if stripped.startswith(("- ", "* ")) else stripped[
                _ORDERED_LIST_ITEM_PREFIX_RE.match(stripped).end():
            ].strip()
            items.append((current, content, ""))
        elif items and current == items[-1][0] and line.startswith(("  ", "\t")) and stripped:
            section, content, continuation = items[-1]
            items[-1] = (section, content, continuation + "\n" + line)
    return items


def is_safe_contract_patch_append(operation: dict, *, anchor_body: str | None = None) -> bool:
    """Validate a bounded section-bound append independent of producer claims."""
    if not isinstance(operation, dict) or operation.get("op", operation.get("kind")) != "append":
        return False
    section, text = operation.get("section"), operation.get("text")
    if not isinstance(text, str) or not text.strip() or "\x00" in text or "\r" in text:
        return False
    lines = text.split("\n")
    if any(not line.strip() or line.lstrip().startswith("##") for line in lines):
        return False
    if section == "Allowed Paths":
        for line in lines:
            match = re.fullmatch(r"- `([^`\n]+)`", line)
            if not match or _normalize_exact_repository_path_literal(match.group(1)) != match.group(1):
                return False
        return True
    if section == "Acceptance Criteria":
        # A structured numbered AC may have indented continuations, or a
        # pre-existing plan may contain several independently numbered ACs.
        # Never promote raw prose (or a new H2) merely because the first line
        # is well formed.
        if not _AC_LINE_RE.fullmatch(lines[0]):
            return False
        return all(
            _AC_LINE_RE.fullmatch(line)
            or (line.startswith(("  ", "\t"))
                and not _CROSS_SECTION_CONTINUATION_RE.match(line.strip()))
            for line in lines[1:]
        )
    if section == "Stop Conditions":
        for line in lines:
            # A pre-existing plan must not bypass the producer's conflicting
            # H2/inline Allowed Paths check by presenting the request as Stop.
            if _ALLOWED_PATHS_DIRECTIVE_RE.search(line):
                return False
            # An inline request marker does not turn its raw-prose payload into
            # a section-bound Stop Condition merely by prefixing the request.
            if _STOP_DIRECTIVE_RE.fullmatch(line.removeprefix("- ").strip()):
                return False
            if any(_is_unsafe_path_literal(token) for token in re.findall(r"`([^`]+)`", line)):
                return False
            if not re.fullmatch(r"-\s+\S.+", line):
                return False
        return True
    if section == "Verification Commands":
        return all(re.fullmatch(
            r"(?:-\s+)?\$\s+\S.+|(?:-\s+)(?:uv|pnpm|python3|node|git|rg)\s+\S.+", line
        ) for line in lines)
    if section in {"In Scope", "Out of Scope"}:
        return all(re.fullmatch(r"-\s+\S.+", line) for line in lines)
    return False


def derive_contract_patch_operations(evidence_list: list, *, source_body: str | None = None) -> list:
    """Map explicit, section-bound OWNER bullets to the existing append wire shape.

    If any recognized directive fails normalization, discard the *whole* plan:
    partial success could record an applied mutation while losing Stop/VC or an
    authorization-bearing path. Background prose with no mapping is ignored.
    """
    operations: list[dict] = []
    for index, evidence in enumerate(evidence_list):
        if not isinstance(evidence, dict):
            continue
        markers = evidence.get("directive_markers") or []
        directives = evidence.get("extracted_directives") or []
        if not markers or not isinstance(directives, list):
            continue
        if source_body is not None:
            records = _structured_comment_items(source_body)
            if [record[1] for record in records] != directives:
                return []  # An evidence/body mismatch cannot authorize a patch.
        else:
            records = [(None, item, "") for item in directives]
        for explicit_section, item, continuation in records:
            if not isinstance(item, str):
                return []
            text = item.strip()
            lowered = text.lower()
            # A numbered AC is already section-bound. Its content can mention
            # "stop conditions" (as in the real #1270 OWNER fixture) without
            # requesting an append to Stop Conditions. Keep the mixed-plan
            # veto for separate, malformed directives later in the records.
            if _AC_LINE_RE.fullmatch(f"- {text}"):
                inline_sections = ["Acceptance Criteria"]
            else:
                inline_sections = [
                    name for name, matches in (
                        ("Allowed Paths", "allowed path" in lowered),
                        ("Verification Commands", "verification command" in lowered),
                        ("Stop Conditions", "stop condition" in lowered),
                    ) if matches
                ]
            # An Allowed Paths heading cannot turn a differently labelled
            # bullet (or a mixed per-bullet request) into path authority. Reject
            # the whole plan, including any earlier valid paths, before emitting.
            if explicit_section == "Allowed Paths" and (
                any(name != "Allowed Paths" for name in inline_sections)
                or _CONFLICTING_PATH_BULLET_LABEL_RE.match(text)
            ):
                return []
            if len(inline_sections) > 1 and (explicit_section is None or explicit_section not in inline_sections):
                return []  # A mixed, ambiguous section request is not a partial plan.
            # An H2 Stop/VC context cannot authorize a path expansion just
            # because its bullet says "Allowed Paths ...". Do not silently
            # append that bullet as a Stop/VC item or apply earlier safe items.
            if explicit_section and explicit_section != "Allowed Paths" and "Allowed Paths" in inline_sections:
                return []
            section = explicit_section or (inline_sections[0] if len(inline_sections) == 1 else None)
            # A Revised AC H2 is context, not an override for a different
            # explicitly labelled bullet. Numbered ACs above still stay ACs,
            # even if their prose happens to mention Stop/VC.
            if (explicit_section == "Acceptance Criteria" and len(inline_sections) == 1
                    and inline_sections[0] in {"Stop Conditions", "Verification Commands"}
                    and _INLINE_SECTION_LABEL_RE.match(text)):
                section = inline_sections[0]
            if section is None and set(markers) <= {"revised ac", "revised acceptance criteria"}:
                section = "Acceptance Criteria"
            if section is None:
                continue
            marker = {
                "Acceptance Criteria": "revised ac" if "revised ac" in markers else "revised acceptance criteria",
                "Stop Conditions": "stop condition",
                "Verification Commands": "verification command",
                "Allowed Paths": "allowed paths",
                "In Scope": "in scope",
                "Out of Scope": "out of scope",
            }[section]
            if section == "Allowed Paths":
                paths = _extract_path_literals_from_text(text)
                if not paths:
                    return []
                for path in paths:
                    operations.append({
                        "section": section, "op": "append", "text": f"- `{path}`",
                        "rationale": "Exact Allowed Paths delta extracted from trusted review comment",
                        "source_evidence_index": index,
                    })
                continue
            if section == "Stop Conditions" and _STOP_DIRECTIVE_RE.fullmatch(text):
                normalized = f"- {text}"
            elif section == "Verification Commands" and (match := _VC_BACKTICK_DIRECTIVE_RE.fullmatch(text)):
                normalized = f"- {match.group('command')}"
            elif section == "Verification Commands" and (match := _VC_DIRECTIVE_RE.fullmatch(text)):
                normalized = match.group(1)
            elif section == "Acceptance Criteria" and _AC_LINE_RE.fullmatch(f"- {text}"):
                normalized = f"- {text}"
            elif section in {"Stop Conditions", "Verification Commands", "In Scope", "Out of Scope"}:
                normalized = f"- {text}"
            else:
                normalized = text
            normalized += continuation
            operation = {
                "section": section, "op": "append", "text": normalized,
                "rationale": f"Directive extracted from trusted review comment ({marker})",
                "source_evidence_index": index,
            }
            if not is_safe_contract_patch_append(operation, anchor_body=source_body):
                return []
            operations.append(operation)
    return operations


_ITERATION_ZERO_STATE_VALUES = {
    "directive_acceptance": {"accepted", "rejected", "unresolved"},
    "repo_fact_verification": {"verified", "contradicted", "unavailable"},
    "external_precondition": {"satisfied", "unsatisfied", "unknown"},
    "contract_update": {"not_needed", "pending", "rebased", "applied", "no_change", "failed"},
}


def _iteration_zero_states(**overrides: str) -> dict:
    """Return the four independent, transaction-local iteration-zero states."""
    states = {
        "directive_acceptance": {"status": "unresolved"},
        "repo_fact_verification": {"status": "unavailable"},
        "external_precondition": {"status": "unknown"},
        "contract_update": {"status": "pending"},
    }
    for key, value in overrides.items():
        if key in _ITERATION_ZERO_STATE_VALUES and value in _ITERATION_ZERO_STATE_VALUES[key]:
            states[key] = {"status": value}
    return states


def normalize_trusted_anchor_iteration_zero(*, repo: str, issue_number: int, anchor: dict, source_body: str) -> dict:
    """Validate trusted-anchor provenance without requiring a prior snapshot.

    This is intentionally a pure, transaction-local bootstrap lane.  It
    consumes the fetched comment metadata, never the caller's interpretation
    of its raw body, and produces source identity used again immediately
    before a controlled mutation.
    """
    parsed = parse_issue_comment_url(anchor.get("html_url") or anchor.get("comment_url"))
    association = anchor.get("author_association")
    source_hash = _sha256(source_body)
    expected_hash = anchor.get("source_body_sha256") or anchor.get("body_sha256")
    states = _iteration_zero_states()
    if (
        parsed is None
        or f"{parsed['owner']}/{parsed['repo']}".lower() != repo.lower()
        or parsed["issue_number"] != issue_number
        or association not in SCOPE_DELTA_AUTHORITY_TRUSTED_ASSOCIATIONS
        or not isinstance(anchor.get("id"), (int, str))
        or str(anchor.get("id")) != parsed["comment_id"]
        or (expected_hash is not None and expected_hash != source_hash)
    ):
        return {
            "accepted": False,
            "states": _iteration_zero_states(directive_acceptance="rejected", contract_update="failed"),
            "failure": "anchor_identity_or_trust_changed",
        }
    states = _iteration_zero_states(
        directive_acceptance="accepted",
        repo_fact_verification="verified",
        external_precondition="satisfied",
        contract_update="pending",
    )
    return {
        "accepted": True,
        "states": states,
        "source_identity": {
            "repo": repo,
            "issue_number": issue_number,
            "comment_id": str(anchor["id"]),
            "source_body_sha256": source_hash,
            "source_span": {"start_line": 1, "end_line": max(1, len(source_body.splitlines()))},
        },
    }


def _replace_top_level_section(body: str, section: str, desired: str) -> str:
    """Replace one H2 section while ignoring headings in fences and quotes."""
    lines = body.splitlines()
    start = None
    end = len(lines)
    in_fence = False
    fence_char = ""
    fence_len = 0
    for index, line in enumerate(lines):
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                continue
        else:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
            continue
        if line.lstrip().startswith(">"):
            continue
        heading = _parse_heading(line)
        if heading == section and start is None:
            start = index
            continue
        if start is not None and heading is not None:
            end = index
            break
    if start is None:
        raise ValueError(f"missing_target_section:{section}")
    replacement = [lines[start]]
    if desired:
        replacement.extend(desired.splitlines())
    if end < len(lines) and (not replacement or replacement[-1] != ""):
        replacement.append("")
    return "\n".join([*lines[:start], *replacement, *lines[end:]]).rstrip() + "\n"


def _insert_top_level_section(body: str, section: str, desired: str, after_section: str | None) -> str:
    """Insert one missing H2 section at an explicit, top-level anchor."""
    rendered = f"## {section}\n{desired.strip()}\n"
    if after_section is None:
        return body.rstrip() + "\n\n" + rendered
    lines = body.splitlines()
    in_fence = False
    fence_char = ""
    fence_len = 0
    insertion = None
    for index, line in enumerate(lines):
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                continue
        elif _is_fence_closer(line, fence_char, fence_len):
            in_fence = False
            continue
        if line.lstrip().startswith(">"):
            continue
        heading = _parse_heading(line)
        if insertion is not None and heading is not None:
            break
        if heading == after_section:
            insertion = index + 1
    if insertion is None:
        raise ValueError(f"missing_section_insertion_anchor:{after_section}")
    while insertion < len(lines) and _parse_heading(lines[insertion]) is None:
        insertion += 1
    replacement = ["", *rendered.rstrip().splitlines(), ""]
    return "\n".join([*lines[:insertion], *replacement, *lines[insertion:]]).rstrip() + "\n"


def build_section_aware_candidate_body(*, body: str, operations: list[dict], source_identity: dict) -> dict:
    """Consume a patch plan into desired H2 section state without fanout.

    Operations are deduplicated by their operation identity.  A caller may
    supply transaction-local ``kind`` (upsert/replace/remove) and
    ``remove_text`` fields; these are deliberately not persisted in the
    existing plan schema.
    """
    sections = extract_sections(body)
    desired_sections = dict(sections)
    identities: set[str] = set()
    postconditions: list[dict] = []
    for operation in operations:
        section = operation.get("section")
        text = operation.get("text")
        kind = operation.get("kind", "upsert")
        if not isinstance(section, str) or not isinstance(text, str) or not text.strip():
            raise ValueError("invalid_section_bound_operation")
        if kind not in {"upsert", "replace", "remove"}:
            raise ValueError("invalid_section_operation_kind")
        # Keep the operation identity as canonical transaction-local data,
        # rather than introducing another digest/artifact format.
        identity = _canonical_json({"section": section, "kind": kind, "desired": text.strip()})
        if identity in identities:
            continue
        identities.add(identity)
        if section not in sections:
            if kind == "remove" or not isinstance(operation.get("after_section"), str):
                raise ValueError("invalid_section_bound_operation")
            desired_sections[section] = text.strip()
            postconditions.append({"section": section, "contains": text.strip(), "removes": None})
            continue
        current = desired_sections[section]
        remove_text = operation.get("remove_text")
        if isinstance(remove_text, str) and remove_text:
            current = "\n".join(line for line in current.splitlines() if remove_text not in line)
        if kind == "remove":
            current = "\n".join(line for line in current.splitlines() if text.strip() not in line)
        elif kind == "replace":
            current = text.strip()
        elif text.strip() not in current.splitlines():
            current = "\n".join(part for part in (current, text.strip()) if part).strip()
        desired_sections[section] = current
        postconditions.append(
            {"section": section, "contains": None if kind == "remove" else text.strip(), "removes": remove_text}
        )

    candidate = body
    for section in sections:
        if desired_sections[section] != sections[section]:
            candidate = _replace_top_level_section(candidate, section, desired_sections[section])
    for operation in operations:
        section = operation.get("section")
        if section not in sections and section in desired_sections:
            candidate = _insert_top_level_section(
                candidate,
                section,
                desired_sections[section],
                operation.get("after_section"),
            )
    return {
        "candidate_body": candidate,
        "changed": candidate != body,
        "operation_identities": sorted(identities),
        "source_identity": source_identity,
        "postconditions": postconditions,
    }


def build_issue_edit_txn_input(
    *,
    issue_number: int,
    repo: str,
    previous_body_sha256: str,
    previous_updated_at: str,
    new_body_file: str,
    readiness_result: dict,
) -> dict:
    """Adapt a candidate to the existing controlled transaction input."""
    return {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": issue_number,
        "repo": repo,
        "new_body_file": new_body_file,
        "expected_previous_body_sha256": previous_body_sha256,
        "expected_previous_updated_at": previous_updated_at,
        "readiness_forwarding_payload": {"readiness_result": readiness_result},
        "comment_mode": {"mode": "skip"},
    }


def run_trusted_anchor_iteration_zero(
    *,
    repo: str,
    issue_number: int,
    issue: dict,
    anchor: dict,
    anchor_body: str,
    patch_plan: dict,
    candidate_readiness,
    fetch_current,
    apply_transaction=None,
    fresh_checks=None,
    allowed_path_deltas: "list | None" = None,
    scope_delta_status: "str | None" = None,
    previous_empty_operations_fingerprints: "list | None" = None,
    reflected_checker=None,
    patch_plan_producer_available: bool = True,
) -> dict:
    """Execute the bounded, callback-based trusted-anchor path.

    The callbacks keep GitHub I/O in the existing controlled executor.  A
    body drift is rebased once; trust/anchor changes and postcondition failure
    are fail-closed.  This function neither creates durable state nor writes
    directly to GitHub.

    #2048 / PR #2057 OWNER REQUEST_CHANGES revision: `allowed_path_deltas` /
    `scope_delta_status` / `previous_empty_operations_fingerprints` /
    `reflected_checker` are optional and default to a no-op (no
    `allowed_path_deltas`) so existing callers that do not pass them keep the
    pre-#2048 "no_change" behavior byte-for-byte.

    When a caller DOES pass a non-empty `allowed_path_deltas` (signalling
    this is an approved Allowed Paths scope reframe, not an ordinary
    contract-key patch) and the candidate body is unchanged (no section-bound
    write would occur), this function:

    1. Calls `fetch_current()` FIRST -- P0-2/P1-4 fix: a stale pre-fetch
       classification could not detect a concurrent anchor edit/trust
       revocation or an Issue body already carrying the approved delta (the
       write path already does this before `apply_transaction`; the no-op
       path previously did not, so `fresh_checks` ran against
       pre-mutation/pre-reread state and issue-editor mutation authority
       could be handed off on stale evidence).
    2. Re-validates anchor identity/trust against the FRESH anchor.
    3. Computes the Allowed-Paths-reflected tri-state via `reflected_checker`
       against the FRESH body (never the pre-fetch `issue` argument).
    4. Calls the single canonical
       `decide_rewrite_route.classify_scope_reframe_disposition()` and
       annotates the result with `rewrite_route` (disposition
       `full_rewrite_required`) or `disposition` (disposition `invalid`).
       `proven_no_change`/`patch` leave the result as an ordinary `no_change`
       no-op (no annotation) -- callers branch on the annotation's presence,
       never construct one themselves.

    `scope_delta_status` is REQUIRED (no longer defaults to
    `"approved_by_trusted_anchor"`) whenever `allowed_path_deltas` is
    non-empty: PR #2057 OWNER review P2 flagged that a caller passing deltas
    but forgetting the status argument would previously be silently treated
    as an approved reframe.

    `patch_plan_producer_available` (PR #2057 OWNER review, iteration 4
    blocker 3): threaded straight into
    `decide_rewrite_route.classify_scope_reframe_disposition()`'s own
    kwarg of the same name. `False` means the freeform
    `SCOPE_DELTA_AUTHORITY_EVIDENCE_V1` producer (`classify_scope_delta_
    authority()`) ran and explicitly could NOT produce an automated patch
    plan for this evidence (`route.action == "human_escalation"`) --
    distinct from "no freeform evidence was ever supplied" (default
    `True`). The caller (`run_refinement_preflight.consume_trusted_anchor_
    contract_patch_plan()`) computes this from the freeform authority
    sidecar BEFORE synthesizing an empty-operations patch plan from the
    structured `ANCHOR_SCOPE_REFRAME_V1` evidence alone, so a producer that
    declined to act is never silently reclassified as a legitimate
    `full_rewrite_required`.
    """
    normalized = normalize_trusted_anchor_iteration_zero(
        repo=repo, issue_number=issue_number, anchor=anchor, source_body=anchor_body
    )
    if not normalized["accepted"]:
        return {"status": "blocked", **normalized, "writes": 0, "iterations": 0}
    if allowed_path_deltas and scope_delta_status is None:
        return {
            "status": "blocked",
            "failure": "scope_delta_status_required_when_allowed_path_deltas_present",
            "writes": 0,
            "iterations": 0,
        }
    current = dict(issue)
    rebases = 0
    while True:
        operations = patch_plan.get("operations", [])
        candidate = build_section_aware_candidate_body(
            body=current.get("body", ""),
            operations=operations,
            source_identity=normalized["source_identity"],
        )
        if not candidate["changed"]:
            if allowed_path_deltas:
                if _SCOPE_REFRAME_ROUTER_IMPORT_FAILED or classify_scope_reframe_disposition is None:
                    return {
                        "status": "environment_failure",
                        "failure": "scope_reframe_router_import_failed",
                        "writes": 0,
                        "iterations": 0,
                    }
                fresh_issue, fresh_anchor = fetch_current()
                fresh_anchor_body = fresh_anchor.get("body", anchor_body)
                fresh_normalized = normalize_trusted_anchor_iteration_zero(
                    repo=repo, issue_number=issue_number, anchor=fresh_anchor, source_body=fresh_anchor_body
                )
                if (
                    not fresh_normalized["accepted"]
                    or fresh_normalized["source_identity"] != normalized["source_identity"]
                ):
                    return {
                        "status": "blocked",
                        "states": _iteration_zero_states(directive_acceptance="rejected", contract_update="failed"),
                        "failure": "anchor_identity_or_trust_changed",
                        "writes": 0,
                        "iterations": 0,
                    }
                fresh_body = fresh_issue.get("body", "")
                reflected_status = (
                    reflected_checker(fresh_body) if reflected_checker is not None else "invalid_or_unavailable"
                )
                decision = classify_scope_reframe_disposition(
                    scope_delta_status=scope_delta_status,
                    operations_key_present=True,
                    operations=list(operations),
                    allowed_path_deltas_raw=list(allowed_path_deltas),
                    reflected_status=reflected_status,
                    anchor_comment_url=anchor.get("html_url") or anchor.get("comment_url"),
                    issue_body_sha256=_sha256(fresh_body),
                    previous_empty_operations_fingerprints=previous_empty_operations_fingerprints,
                    patch_plan_producer_available=patch_plan_producer_available,
                )
                result = {
                    "status": "invalid" if decision.disposition == "invalid" else "no_change",
                    "states": _iteration_zero_states(contract_update="no_change"),
                    "writes": 0,
                    "iterations": 0,
                    **candidate,
                }
                if decision.disposition == "full_rewrite_required":
                    result["rewrite_route"] = decision.to_dict()
                elif decision.disposition == "invalid":
                    result["disposition"] = decision.to_dict()
                if fresh_checks is not None:
                    result["fresh_checks"] = fresh_checks(fresh_issue)
                return result
            result = {
                "status": "no_change",
                "states": _iteration_zero_states(contract_update="no_change"),
                "writes": 0,
                "iterations": 0,
                **candidate,
            }
            if fresh_checks is not None:
                result["fresh_checks"] = fresh_checks(current)
            return result
        readiness = candidate_readiness(candidate["candidate_body"])
        if not isinstance(readiness, dict) or readiness.get("status") != "go":
            return {
                "status": "blocked",
                "states": _iteration_zero_states(contract_update="failed"),
                "failure": "candidate_readiness_not_go",
                "writes": 0,
                "iterations": rebases,
                **candidate,
            }
        fresh_issue, fresh_anchor = fetch_current()
        fresh_anchor_body = fresh_anchor.get("body", anchor_body)
        fresh_normalized = normalize_trusted_anchor_iteration_zero(
            repo=repo, issue_number=issue_number, anchor=fresh_anchor, source_body=fresh_anchor_body
        )
        if not fresh_normalized["accepted"] or fresh_normalized["source_identity"] != normalized["source_identity"]:
            return {
                "status": "blocked",
                "states": _iteration_zero_states(directive_acceptance="rejected", contract_update="failed"),
                "failure": "anchor_identity_or_trust_changed",
                "writes": 0,
                "iterations": rebases,
            }
        if fresh_issue.get("body") != current.get("body"):
            if rebases >= 1:
                return {
                    "status": "blocked",
                    "states": _iteration_zero_states(contract_update="failed"),
                    "failure": "body_drift_retry_exhausted",
                    "writes": 0,
                    "iterations": rebases,
                }
            current = dict(fresh_issue)
            rebases += 1
            continue
        if apply_transaction is None:
            return {
                "status": "ready_for_controlled_mutation",
                "states": _iteration_zero_states(contract_update="pending"),
                "writes": 0,
                "iterations": rebases,
                "readiness": readiness,
                **candidate,
            }
        transaction_result = apply_transaction(fresh_issue, candidate["candidate_body"], readiness)
        final_issue, final_anchor = fetch_current()
        final_anchor_body = final_anchor.get("body", anchor_body)
        final_normalized = normalize_trusted_anchor_iteration_zero(
            repo=repo, issue_number=issue_number, anchor=final_anchor, source_body=final_anchor_body
        )
        if not final_normalized["accepted"] or final_issue.get("body") != candidate["candidate_body"]:
            return {
                "status": "blocked",
                "states": _iteration_zero_states(contract_update="failed"),
                "failure": "final_readback_postcondition_failed",
                "writes": 1,
                "iterations": rebases,
            }
        if any(
            (item["contains"] is not None and item["contains"] not in final_issue["body"])
            or (item["removes"] and item["removes"] in final_issue["body"])
            for item in candidate["postconditions"]
        ):
            return {
                "status": "blocked",
                "states": _iteration_zero_states(contract_update="failed"),
                "failure": "section_postcondition_failed",
                "writes": 1,
                "iterations": rebases,
            }
        result = {
            "status": "applied",
            "states": _iteration_zero_states(contract_update="applied"),
            "writes": 1,
            "iterations": rebases,
            "transaction_result": transaction_result,
            **candidate,
        }
        if fresh_checks is not None:
            result["fresh_checks"] = fresh_checks(final_issue)
        return result


def _patch_source_evidence_entry(evidence: dict) -> dict:
    # PR #1332 review fix (P1): carry source_comment_id / extracted_text_sha256
    # / captured_at so issue-author/edit-issue can detect a stale or
    # mismatched source comment before applying an operation derived from it.
    # extracted_text_sha256 hashes only the already-extracted directive
    # texts (never the raw comment body, AC14).
    extracted_directives = evidence.get("extracted_directives") or []
    extracted_text_sha256 = _sha256("\n".join(extracted_directives)) if extracted_directives else None
    return {
        "source_ref": evidence.get("source_ref") or evidence.get("comment_url"),
        "source_body_sha256": evidence.get("body_sha256"),
        "author_association": evidence.get("author_association"),
        "source_comment_id": evidence.get("comment_id"),
        "extracted_text_sha256": extracted_text_sha256,
        "captured_at": evidence.get("captured_at"),
        "start_line": evidence.get("start_line"),
        "end_line": evidence.get("end_line"),
    }


def _provenance_from_evidence(evidence: "dict | None") -> dict:
    evidence = evidence or {}
    return {
        "source_kind": evidence.get("source_kind"),
        "source_ref": evidence.get("source_ref") or evidence.get("comment_url"),
        "body_sha256": evidence.get("body_sha256"),
        "author_association": evidence.get("author_association"),
    }


def _directive_from_evidence(evidence: "dict | None", confidence: "str | None") -> dict:
    evidence = evidence or {}
    return {
        "confidence": confidence,
        "extracted_markers": list(evidence.get("directive_markers") or []),
    }


def _build_scope_delta_authority_result(
    *,
    authority_category: str,
    provenance: dict,
    directive: dict,
    boundary_flags: dict,
    route_action: str,
    reason_code,
    implementation_allowed: bool,
    next_step,
) -> dict:
    return {
        "schema_version": SCOPE_DELTA_AUTHORITY_SCHEMA_VERSION,
        "authority_category": authority_category,
        "provenance": provenance,
        "directive": directive,
        "boundary_flags": {key: bool(boundary_flags.get(key, False)) for key in _BOUNDARY_FLAG_KEYS},
        "route": {
            "action": route_action,
            "reason_code": reason_code,
            "implementation_allowed": implementation_allowed,
            "next_step": next_step,
        },
    }


_BOUNDARY_REASON_PRIORITY = (
    ("destructive_or_non_idempotent_operation", REASON_DESTRUCTIVE_OR_NON_IDEMPOTENT_OPERATION),
    ("changes_external_service_boundary", REASON_CHANGES_EXTERNAL_SERVICE_BOUNDARY),
    ("requires_issue_split", REASON_REQUIRES_ISSUE_SPLIT),
    ("changes_permission_boundary", REASON_CHANGES_PERMISSION_BOUNDARY),
    ("expands_allowed_paths", REASON_EXPANDS_ALLOWED_PATHS),
)


def _is_negated_permission_text(text: str) -> bool:
    lowered = (text or "").lower()
    for pattern in _DIRECTIVE_NEGATION_PATTERNS:
        if pattern.search(lowered):
            return True
    return False


def _contains_all_permission_constraints(text: str) -> bool:
    lowered = (text or "").lower()
    if _is_negated_permission_text(lowered):
        return False
    required_groups = (
        ("exact permission delta", "exact delta", "exact に", "正確な権限差分", "exact diff"),
        (
            "necessary for the human directive",
            "necessary for the directive",
            "required for the directive",
            "human directive の目的に必要",
            "指示の目的に必要",
        ),
        ("least privilege", "least-privilege", "最小権限"),
        ("non-destructive", "非破壊", "not destructive", "破壊的でない"),
        ("no secret", "no secrets", "secretなし", "secretsなし", "シークレットなし"),
        ("no paid", "no external paid", "external-paidなし", "外部課金なし", "課金なし"),
        (
            "no unrelated privilege widening",
            "no unrelated widening",
            "unrelated privilege wideningなし",
            "無関係な権限拡大なし",
        ),
    )
    return all(any(marker in lowered for marker in group) for group in required_groups)


def _permission_boundary_is_constrained_directive(evidence: dict, boundary_flags: dict) -> bool:
    """Return whether a permission redesign is explicitly constrained.

    Permission-boundary work remains fail-closed by default. The existing
    evidence schema intentionally carries only normalized directive text, so
    this predicate uses that already-extracted text and never requests raw
    comment bodies or adds a parallel authorization schema. Every safety
    statement is required; omission is not treated as approval.
    """
    if not boundary_flags.get("changes_permission_boundary"):
        return False
    if boundary_flags.get("destructive_or_non_idempotent_operation") or boundary_flags.get(
        "requires_issue_split"
    ) or boundary_flags.get("changes_external_service_boundary"):
        return False

    directives = evidence.get("extracted_directives") or []
    concrete_deltas: list[tuple[str, str, str]] = []
    for directive in directives:
        candidate = _strip_quoted_fragments(str(directive))
        if not _contains_all_permission_constraints(candidate):
            continue
        if _UNRELATED_PERMISSION_WIDENING_RE.search(candidate):
            return False
        # A stock claim of an "exact permission delta" is insufficient.
        # The normalized directive must carry an actual before→after value
        # and an exact repository-relative target path. The controlled
        # transaction and fresh validators verify that the proposed delta is
        # still current before any implementation route is considered.
        for delta in _CONCRETE_PERMISSION_DELTA_RE.finditer(candidate):
            path = _CONCRETE_REPOSITORY_PATH_RE.search(str(directive))
            if path is None or not delta.group("before").strip() or not delta.group("after").strip():
                continue
            concrete_deltas.append(
                (delta.group("before").strip(), delta.group("after").strip(), path.group("path"))
            )

    # A trusted directive may name exactly one verified permission delta. A
    # second (including contradictory) delta requires a human resolution;
    # choosing one would be an inference across authorization boundaries.
    return len(concrete_deltas) == 1


def _has_investigation_derived_allowed_path_literals(
    investigation_derived_path_literals: "list | None",
) -> bool:
    """#2086 AC3/AC4: agent read-only investigation may supply exact
    Allowed Paths literals for a trusted operator-selected human-context
    directive that names architecture/workflow-level scope expansion in
    semantic prose without an exact backtick path literal in the directive
    text itself (Design Invariant #2: the human states *what* must change,
    the agent derives *which exact repo paths* implement that change from
    current-main evidence).

    This is deliberately a **caller-supplied, out-of-evidence** argument
    (never embedded into ``SCOPE_DELTA_AUTHORITY_EVIDENCE_V1``, whose schema
    is out of this Issue's Allowed Paths and stays ``additionalProperties:
    false``). The caller (``run_refinement_preflight.py`` / the
    ``codebase-investigator`` SubAgent-backed orchestrator step) is
    responsible for only ever populating this from read-only repository
    inventory, never from the raw comment body itself.
    """
    if not isinstance(investigation_derived_path_literals, list):
        return False
    if not investigation_derived_path_literals:
        return False
    # #2086 AC4 P0 fix_delta: this was previously an `any()` check -- a
    # SINGLE syntactically-valid entry anywhere in the list cleared the
    # boundary even if the list also contained an unsafe/malformed token
    # (e.g. `["docs/dev/workflow.md", "../../escape.py"]`). That is an
    # authority-laundering risk the moment a caller actually wires this
    # parameter through (see run_refinement_preflight.py / #2086 AC3): a
    # single genuine investigation result mixed with ONE attacker- or
    # bug-supplied unsafe token would still clear `expands_allowed_paths`.
    # Fail-closed on mixed input: EVERY entry must be a safe, non-empty
    # string that normalizes to an exact repository-relative path literal,
    # or the entire set is untrusted and the boundary is NOT cleared.
    for item in investigation_derived_path_literals:
        if not isinstance(item, str):
            return False
        if _normalize_exact_repository_path_literal(item) is None:
            return False
    return True


def _first_blocking_boundary_reason(
    boundary_flags: dict,
    evidence: dict,
    *,
    operator_asserted_human_context: bool = False,
    investigation_derived_path_literals: "list | None" = None,
):
    """Return the first boundary that has not earned its narrow exception.

    Every true flag is evaluated independently.  Clearing a constrained
    permission redesign must never hide an external, destructive, split, or
    vague Allowed Paths boundary that appears later in the priority order.

    #2086 AC3/AC4: for the operator-selected human-context lane only
    (``operator_asserted_human_context=True``, gated upstream by
    ``classify_scope_delta_authority()`` on ``authority_category ==
    "human_review_directive"`` -- i.e. a trusted OWNER/MEMBER/COLLABORATOR
    on the ``with_human_context`` lane), a safe
    ``investigation_derived_path_literals`` entry clears the
    ``expands_allowed_paths`` boundary the same way an exact backtick
    literal already extracted from the directive text does. This does not
    relax the destructive / permission / external-service / issue-split
    boundaries, and it does not relax the existing #1952 "mixed literal"
    fail-closed behavior for directive text that itself contains an unsafe
    or malformed backtick token (``_has_explicit_exact_allowed_path_expansion``
    is still tried first and is unaffected by this addition).
    """
    for key, reason in _BOUNDARY_REASON_PRIORITY:
        if not boundary_flags.get(key):
            continue
        if key == "changes_permission_boundary" and _permission_boundary_is_constrained_directive(
            evidence, boundary_flags
        ):
            continue
        if key == "expands_allowed_paths" and (
            _has_explicit_exact_allowed_path_expansion(evidence)
            or (
                operator_asserted_human_context
                and _has_investigation_derived_allowed_path_literals(
                    investigation_derived_path_literals
                )
            )
        ):
            continue
        return reason
    return None


def _classify_single_evidence(evidence: dict) -> dict:
    source_kind = evidence.get("source_kind")
    author_association = evidence.get("author_association")
    markers = evidence.get("directive_markers") or []
    directives = evidence.get("extracted_directives") or []
    boundary_names = set(evidence.get("boundary_flags") or [])
    confidence = evidence.get("confidence")
    if confidence not in (
        DIRECTIVE_CONFIDENCE_EXPLICIT,
        DIRECTIVE_CONFIDENCE_AMBIGUOUS,
        DIRECTIVE_CONFIDENCE_CONFLICTING,
        DIRECTIVE_CONFIDENCE_INFERRED,
    ):
        if markers and directives:
            confidence = DIRECTIVE_CONFIDENCE_EXPLICIT
        elif markers:
            confidence = DIRECTIVE_CONFIDENCE_AMBIGUOUS
        else:
            confidence = DIRECTIVE_CONFIDENCE_INFERRED

    untrusted = False
    if source_kind == "generated_by_agent":
        category = AUTHORITY_CATEGORY_AI_INFERRED
    elif source_kind == "parent_issue":
        category = AUTHORITY_CATEGORY_EXISTING_PARENT_CONTRACT
    elif source_kind == "related_issue":
        category = AUTHORITY_CATEGORY_RELATED_ISSUE_DEPENDENCY
    elif source_kind in ("issue_comment", "pull_request_review"):
        if author_association in SCOPE_DELTA_AUTHORITY_TRUSTED_ASSOCIATIONS:
            category = AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE
        else:
            # AC13: fail-closed for CONTRIBUTOR / NONE / bot / missing association.
            category = AUTHORITY_CATEGORY_AI_INFERRED
            untrusted = True
    else:
        category = AUTHORITY_CATEGORY_AI_INFERRED

    return {
        "authority_category": category,
        "confidence": confidence,
        "boundary_names": boundary_names,
        "untrusted": untrusted,
    }


def _has_conflicting_directives(evidence_list: list) -> bool:
    """AC17: multiple trusted reviewers proposing different explicit directives."""
    directive_sets = []
    for evidence in evidence_list:
        directives = evidence.get("extracted_directives") or []
        if directives:
            directive_sets.append(frozenset(d.strip().lower() for d in directives))
    if len(directive_sets) < 2:
        return False
    first = directive_sets[0]
    return any(candidate != first for candidate in directive_sets[1:])


def _parsed_issue_number_for_cross_target(evidence: dict, expected_repo) -> "int | None":
    """#2053 AC6: resolve the issue number a comment-shaped evidence entry
    structurally targets, restricted to issue_comment/pull_request_review
    kinds (the only kinds that carry a comment_url). Returns None when the
    evidence is not comment-shaped, the URL does not parse, or (when
    expected_repo is provided) the URL points at a different repository --
    those cases are handled by the existing same-target validation path,
    not by cross-target detection.
    """
    if evidence.get("source_kind") not in ("issue_comment", "pull_request_review"):
        return None
    parsed = parse_issue_comment_url(evidence.get("comment_url"))
    if parsed is None:
        return None
    if expected_repo:
        expected_parts = expected_repo.lower().split("/", 1)
        if [parsed["owner"].lower(), parsed["repo"].lower()] != expected_parts:
            return None
    return parsed["issue_number"]


def _build_cross_target_follow_up_candidate(evidence: dict, target_issue_number: "int | None") -> dict:
    """#2053 AC6: a non-mutating, informational candidate. mutation_authority
    is always False -- a follow-up candidate never grants Issue mutation
    authority on the cross-referenced target; it requires that target
    Issue's own trusted directive to become actionable.
    """
    return {
        "target_issue_number": target_issue_number,
        "source_kind": evidence.get("source_kind"),
        "comment_url": evidence.get("comment_url"),
        "mutation_authority": False,
        "note": "cross_target_directive_observed_requires_own_trusted_directive_on_target_issue",
    }


def classify_scope_delta_authority(
    evidence,
    *,
    triggered: bool = True,
    target_issue_number=None,
    base_issue_body_sha256=None,
    expected_repo=None,
    investigation_derived_path_literals=None,
    source_body: str | None = None,
) -> dict:
    """#2053 AC6 public wrapper: partitions `evidence` into same-target and
    cross-target (a different Issue than `target_issue_number`) before
    delegating to `_classify_scope_delta_authority_core`.

    A cross-target comment attempting a directive (non-empty
    directive_markers/extracted_directives) is NEVER treated as mutation
    authority for `target_issue_number` -- `cross_target_direct_mutation`
    is always rejected (AC6). It is instead surfaced as a non-mutating
    `follow_up_candidates` entry so it is not silently dropped, matching
    AC5's fail-closed cross-issue behavior while adding AC6's explicit type
    separation from `primary_target_exact_delta`.
    """
    if evidence is None:
        raw_list = []
    elif isinstance(evidence, dict):
        raw_list = [evidence]
    elif isinstance(evidence, list):
        raw_list = [item for item in evidence if isinstance(item, dict)]
    else:
        raw_list = []

    cross_target_follow_ups: list = []
    same_target_list: list = []
    if target_issue_number is not None and triggered:
        for item in raw_list:
            parsed_issue = _parsed_issue_number_for_cross_target(item, expected_repo)
            if parsed_issue is not None and parsed_issue != target_issue_number:
                if item.get("extracted_directives") or item.get("directive_markers"):
                    cross_target_follow_ups.append(
                        _build_cross_target_follow_up_candidate(item, parsed_issue)
                    )
                continue
            same_target_list.append(item)
    else:
        same_target_list = raw_list

    filtered_evidence = same_target_list if raw_list else evidence

    result = _classify_scope_delta_authority_core(
        filtered_evidence,
        triggered=triggered,
        target_issue_number=target_issue_number,
        base_issue_body_sha256=base_issue_body_sha256,
        expected_repo=expected_repo,
        investigation_derived_path_literals=investigation_derived_path_literals,
        source_body=source_body,
    )

    if cross_target_follow_ups:
        result = dict(result)
        result["follow_up_candidates"] = cross_target_follow_ups
        result["route"] = dict(result["route"])
        if not same_target_list:
            # Pure cross-target attempt: no same-target evidence survived,
            # so the only signal is the rejected cross-target mutation.
            result["route"]["reason_code"] = REASON_CROSS_TARGET_DIRECT_MUTATION
            result["route"]["target_binding"] = "cross_target_direct_mutation"
            result["route"]["implementation_allowed"] = False
        elif result["route"].get("action") == SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED:
            result["route"]["target_binding"] = "primary_target_exact_delta"

    return result


def _classify_scope_delta_authority_core(
    evidence,
    *,
    triggered: bool = True,
    target_issue_number=None,
    base_issue_body_sha256=None,
    expected_repo=None,
    investigation_derived_path_literals=None,
    source_body: str | None = None,
) -> dict:
    """AC1-AC19: classify scope_delta_authority for a scope signal delta.

    `evidence` is normalized SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 (single dict or
    a list for multiple reviewers). Never parses raw comment bodies -- only
    consumes already-extracted markers/directives/boundary_flags (AC14).

    `expected_repo` (PR #1332 review fix, P0/P1): the `owner/name` repo the
    caller expects evidence to originate from. It is forwarded to
    validate_scope_delta_authority_evidence_url() so that a same-issue-number
    URL from a *different* repository is fail-closed rejected (AC16
    hardening). When omitted, issue_comment evidence is fail-closed rejected
    by the URL validator (repo cross-check cannot be skipped silently).

    `investigation_derived_path_literals` (#2086 AC3/AC4): an optional list
    of repository-relative path strings derived by *read-only* agent
    investigation (never from the raw comment body). It is only ever
    consulted for the operator-selected human-context lane (evidence whose
    `authority_category` resolves to `human_review_directive`) to clear the
    `expands_allowed_paths` boundary when the directive itself names
    architecture/workflow-level scope expansion in prose without an exact
    backtick literal (see `_has_investigation_derived_allowed_path_literals`).
    """
    if not triggered:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence(None),
            directive=_directive_from_evidence(None, None),
            boundary_flags={},
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_NOT_TRIGGERED,
            reason_code=None,
            implementation_allowed=True,
            next_step=None,
        )

    if evidence is None:
        evidence_list = []
    elif isinstance(evidence, dict):
        evidence_list = [evidence]
    elif isinstance(evidence, list):
        evidence_list = [item for item in evidence if isinstance(item, dict)]
    else:
        evidence_list = []

    # AC16: drop any evidence entry whose comment_url fails structural
    # validation against the target issue (fail-closed, not raised).
    evidence_list = [
        item
        for item in evidence_list
        if validate_scope_delta_authority_evidence_url(
            item, target_issue_number=target_issue_number, expected_repo=expected_repo
        )
    ]

    if not evidence_list:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence({"source_kind": "generated_by_agent"}),
            directive=_directive_from_evidence(None, DIRECTIVE_CONFIDENCE_INFERRED),
            boundary_flags={},
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_AI_INFERRED_SCOPE_DELTA,
            implementation_allowed=False,
            next_step=None,
        )

    classified = [_classify_single_evidence(item) for item in evidence_list]
    primary = classified[0]
    primary_evidence = evidence_list[0]

    all_boundary_names = set()
    for item in classified:
        all_boundary_names |= item["boundary_names"]
    boundary_flags = {key: (key in all_boundary_names) for key in _BOUNDARY_FLAG_KEYS}

    if primary["untrusted"]:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_INFERRED),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_UNTRUSTED_AUTHOR_ASSOCIATION,
            implementation_allowed=False,
            next_step=None,
        )

    category = primary["authority_category"]

    if category in (AUTHORITY_CATEGORY_EXISTING_PARENT_CONTRACT, AUTHORITY_CATEGORY_RELATED_ISSUE_DEPENDENCY):
        return _build_scope_delta_authority_result(
            authority_category=category,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, primary["confidence"]),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_NOT_TRIGGERED,
            reason_code=None,
            implementation_allowed=True,
            next_step=None,
        )

    if category == AUTHORITY_CATEGORY_AI_INFERRED:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, primary["confidence"]),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_AI_INFERRED_SCOPE_DELTA,
            implementation_allowed=False,
            next_step=None,
        )

    # category == human_review_directive
    boundary_reason = _first_blocking_boundary_reason(
        boundary_flags,
        primary_evidence,
        operator_asserted_human_context=True,
        investigation_derived_path_literals=investigation_derived_path_literals,
    )
    if boundary_reason is not None:
        # AC18: boundary flags gate even a trusted approval.
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, primary["confidence"]),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=boundary_reason,
            implementation_allowed=False,
            next_step=None,
        )

    if _has_conflicting_directives(evidence_list):
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_CONFLICTING),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_CONFLICTING_HUMAN_DIRECTIVES,
            implementation_allowed=False,
            next_step=None,
        )

    confidence = primary["confidence"]
    if confidence == DIRECTIVE_CONFIDENCE_EXPLICIT:
        # PR #1332 review fix (P1): a CONTRACT_PATCH_PLAN_V1 can only be
        # generated against a resolved Issue body snapshot. Without
        # base_issue_body_sha256, claiming contract_update_required would
        # imply "safe to proceed" while the patch plan itself cannot be
        # safely built (build_contract_patch_plan_v1 fail-closes on this) --
        # so fail closed to human_escalation instead of raising.
        if not base_issue_body_sha256:
            return _build_scope_delta_authority_result(
                authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
                provenance=_provenance_from_evidence(primary_evidence),
                directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_EXPLICIT),
                boundary_flags=boundary_flags,
                route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
                reason_code=REASON_MISSING_BASE_ISSUE_BODY_SHA256,
                implementation_allowed=False,
                next_step=None,
            )
        result = _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_EXPLICIT),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED,
            reason_code=REASON_EXPLICIT_HUMAN_CONTRACT_DIRECTIVE,
            implementation_allowed=False,
            next_step=NEXT_STEP_RERUN_REFINEMENT_AFTER_CONTRACT_UPDATE,
        )
        result["contract_patch_plan"] = build_contract_patch_plan_v1(
            target_issue_number=target_issue_number,
            base_issue_body_sha256=base_issue_body_sha256,
            source_evidence=[_patch_source_evidence_entry(item) for item in evidence_list],
            operations=(derive_contract_patch_operations(evidence_list, source_body=source_body)
                        if source_body is None or (
                            len(evidence_list) == 1
                            and evidence_list[0].get("body_sha256") == hashlib.sha256(
                                source_body.encode("utf-8")
                            ).hexdigest()
                        ) else []),
        )
        return result

    # ambiguous / inferred confidence for a human-sourced comment
    return _build_scope_delta_authority_result(
        authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
        provenance=_provenance_from_evidence(primary_evidence),
        directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_AMBIGUOUS),
        boundary_flags=boundary_flags,
        route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
        reason_code=REASON_AMBIGUOUS_HUMAN_DIRECTIVE,
        implementation_allowed=False,
        next_step=None,
    )
