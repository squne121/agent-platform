#!/usr/bin/env python3
"""
check_issue_contract.py — C1〜C11 の決定論的チェッカー

Issue 本文（Markdown テキスト）を読み、C1〜C11 の判定を機械的に行い
JSON で結果を出力する。LLM は本スクリプトの JSON 出力を整形するだけでよい。

Usage:
    # ファイルから読み込む（テスト用）
    python check_issue_contract.py --file <path>

    # GitHub から取得する
    python check_issue_contract.py --issue <number> --repo <owner/repo>

    # JSON 出力
    python check_issue_contract.py --file <path> --json

Caller contract for --json mode:
    - stdout carries the contract-check JSON ONLY (a single final JSON object).
    - stderr carries diagnostics (warnings, deprecation notices, progress output).
    - Callers MUST parse JSON from stdout only and MUST NOT merge stderr into
      stdout (e.g. do not use stderr=subprocess.STDOUT). Treating stderr
      diagnostics as part of the JSON payload will break parsing.

Exit codes:
    0: すべてのチェックが pass（verdict: approve）
    1: 1 つ以上のチェックが fail（verdict: needs-fix）
    2: 入力エラー / 実行エラー
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional


# ---------------------------------------------------------------------------
# prose_boundary_policy の heading_policy を import（#654）
# ---------------------------------------------------------------------------
# check_issue_contract.py の scripts/ は review-issue/scripts/ にあるが、
# prose_boundary_policy.py は create-issue/scripts/ にある。
# sys.path に create-issue/scripts/ を追加してから import する。
_CREATE_ISSUE_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "create-issue" / "scripts"
)
if str(_CREATE_ISSUE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS))

# #1135: shared, section-bound MRC parser + repo path-policy SSOT
from mrc_contract_parser import (  # noqa: E402
    REASON_MISSING as _MRC_REASON_MISSING,
    parse_machine_readable_contract,
)
from path_classification import (  # noqa: E402
    extract_allowed_paths as pc_extract_allowed_paths,
    has_code_or_runtime_scope,
    is_docs_only_allowed_paths,
)

# ---------------------------------------------------------------------------
# vc_contract_syntax を import（#993: shared VC grammar parser）
# ---------------------------------------------------------------------------
_VC_SYNTAX_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "issue-contract-review" / "scripts"
)
if str(_VC_SYNTAX_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_VC_SYNTAX_SCRIPTS))
_IMPL_REVIEW_LOOP_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "impl-review-loop" / "scripts"
)
if str(_IMPL_REVIEW_LOOP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_IMPL_REVIEW_LOOP_SCRIPTS))

try:
    from vc_contract_syntax import parse_verification_commands_section as _parse_vc_section
    _VC_SECTION_PARSER_AVAILABLE = True
except ImportError:
    _VC_SECTION_PARSER_AVAILABLE = False

    def _parse_vc_section(vc_section: str):  # type: ignore[misc]
        return None

try:
    from evaluate_product_spec_gate import evaluate_product_spec_payload  # noqa: E402
except ImportError:
    evaluate_product_spec_payload = None  # type: ignore[assignment]

try:
    from prose_boundary_policy import (
        lookup_heading_policy as _lookup_heading_policy,
        parse_atx_heading_line as _parse_atx_heading_line,
        iter_markdown_blocks as _iter_markdown_blocks,
        BLOCK_KIND_CODE_FENCE as _BLOCK_KIND_CODE_FENCE,
    )
    _HEADING_POLICY_AVAILABLE = True
except ImportError:
    _HEADING_POLICY_AVAILABLE = False

    def _lookup_heading_policy(heading_text: str):  # type: ignore[misc]
        return None

    def _parse_atx_heading_line(line: str):  # type: ignore[misc]
        return None

    def _iter_markdown_blocks(text: str):  # type: ignore[misc]
        """Fallback: yield entire text as a single prose block."""
        if text:
            yield text, "human_prose"

    _BLOCK_KIND_CODE_FENCE = "code_fence"


# ---------------------------------------------------------------------------
# ISSUE_KIND_POLICY_V1 SSOT loader
# ---------------------------------------------------------------------------
# Canonical source: docs/dev/github-ops.md ## ISSUE_KIND_POLICY_V1
# Local allowlist definitions are prohibited (SSOT single-source rule).

_ISSUE_KIND_POLICY_CACHE: "dict | None" = None


def _find_repo_root_for_contract() -> Path:
    """Find repository root by walking up to find .git directory."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume .claude/skills/review-issue/scripts/
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def _load_extension_surface_policy_matcher():
    """Dynamically load `scripts/agent-guards/extension_surface_policy_matcher.py`.

    Mirrors the existing dynamic-load pattern in
    `.claude/skills/issue-contract-review/scripts/declared_path_overlap.py`
    (Issue #2290 "Notes for Reviewer": no new static import boundary /
    package). Returns `None` if the shared evaluator module cannot be
    located, so callers degrade to CheckResult.NA rather than raising.
    """
    import importlib.util

    matcher_path = (
        _find_repo_root_for_contract() / "scripts" / "agent-guards" / "extension_surface_policy_matcher.py"
    )
    if not matcher_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "extension_surface_policy_matcher_for_check_issue_contract", matcher_path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_repo_identity():
    """Import `scripts/agent-guards/repo_identity.py` (single repository identity SSOT)."""
    guards = _find_repo_root_for_contract() / "scripts" / "agent-guards"
    if str(guards) not in sys.path:
        sys.path.insert(0, str(guards))
    import repo_identity

    return repo_identity


class IssueKindPolicyLoadError(RuntimeError):
    """Raised when ISSUE_KIND_POLICY_V1 cannot be loaded from SSOT.

    Fail-closed: callers must not silently substitute a hardcoded fallback.
    If this exception escapes to detect_issue_kind, it returns UNKNOWN_ISSUE_KIND_SENTINEL.
    """


def _load_issue_kind_policy(repo_root: "Path | None" = None) -> dict:
    """Load ISSUE_KIND_POLICY_V1 from docs/dev/github-ops.md.

    Returns a dict with keys:
      - canonical_kinds: frozenset[str]
      - aliases: dict[str, str]
      - unknown_kind_policy: str  ("block")
      - unknown_kind_reason_code: str

    Raises IssueKindPolicyLoadError if the SSOT file is missing, the
    ISSUE_KIND_POLICY_V1 block cannot be found/parsed, or the yaml library
    is unavailable.  No silent fallback — callers must handle the error.
    """
    global _ISSUE_KIND_POLICY_CACHE
    if _ISSUE_KIND_POLICY_CACHE is not None:
        return _ISSUE_KIND_POLICY_CACHE

    if repo_root is None:
        repo_root = _find_repo_root_for_contract()

    ssot_path = repo_root / "docs" / "dev" / "github-ops.md"
    if not ssot_path.exists():
        raise IssueKindPolicyLoadError(
            f"SSOT file not found: {ssot_path}. "
            "Cannot load ISSUE_KIND_POLICY_V1 — fail-closed."
        )

    try:
        import yaml as _yaml
    except ImportError as exc:
        raise IssueKindPolicyLoadError(
            "PyYAML is not available; cannot parse ISSUE_KIND_POLICY_V1."
        ) from exc

    try:
        text = ssot_path.read_text(encoding="utf-8")
        match = re.search(r"```yaml\s*\nISSUE_KIND_POLICY_V1:(.*?)```", text, re.DOTALL)
        if not match:
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 fenced YAML block not found in {ssot_path}. "
                "Ensure the block starts with ```yaml on a line followed by 'ISSUE_KIND_POLICY_V1:'."
            )

        yaml_content = "ISSUE_KIND_POLICY_V1:" + match.group(1)
        parsed = _yaml.safe_load(yaml_content)
        if not isinstance(parsed, dict) or "ISSUE_KIND_POLICY_V1" not in parsed:
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 YAML parse produced unexpected structure in {ssot_path}."
            )

        policy = parsed["ISSUE_KIND_POLICY_V1"]
        if not isinstance(policy, dict):
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 value is not a mapping in {ssot_path}."
            )

        canonical_kinds = frozenset(policy.get("canonical_kinds") or [])
        aliases_raw = policy.get("aliases") or {}
        aliases = {str(k): str(v) for k, v in aliases_raw.items()} if isinstance(aliases_raw, dict) else {}
        unknown_kind_policy = str(policy.get("unknown_kind_policy", "block"))
        unknown_kind_reason_code = str(policy.get("unknown_kind_reason_code", "unknown_issue_kind"))

        result: dict = {
            "canonical_kinds": canonical_kinds,
            "aliases": aliases,
            "unknown_kind_policy": unknown_kind_policy,
            "unknown_kind_reason_code": unknown_kind_reason_code,
        }
        _ISSUE_KIND_POLICY_CACHE = result
        return result
    except IssueKindPolicyLoadError:
        raise
    except Exception as exc:
        raise IssueKindPolicyLoadError(
            f"Unexpected error while loading ISSUE_KIND_POLICY_V1 from {ssot_path}: {exc}"
        ) from exc


def _clear_issue_kind_policy_cache() -> None:
    """Clear the SSOT cache (for testing only)."""
    global _ISSUE_KIND_POLICY_CACHE
    _ISSUE_KIND_POLICY_CACHE = None


# Sentinel value returned by detect_issue_kind when kind is not in SSOT allowlist/aliases.
UNKNOWN_ISSUE_KIND_SENTINEL = "unknown_issue_kind"


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    NA = "n/a"
    LEGACY_MISSING = "legacy_missing_applicability"


class PreflightScope(str, Enum):
    """Valid values for # preflight-scope: annotation on VC commands."""
    PR_REVIEW_ONLY = "pr_review_only"
    RUNTIME_ONLY = "runtime_only"
    UNKNOWN = "unknown"  # fail-closed / human_judgment


REVIEW_ISSUE_RESULT_SCHEMA_VERSION = "review_issue_result/v1"
REVIEW_ISSUE_RESULT_SCHEMA = "REVIEW_ISSUE_RESULT_V1"
REVIEW_ISSUE_RESULT_SCHEMA_FILE = (
    Path(__file__).resolve().parent.parent / "schemas" / "review_issue_result_v1.json"
)
# ISSUE_CONTRACT_READINESS_RESULT_V1 は contract_readiness_check.py --mode execute
# の producer schema。readiness_error_to_structured_blocker() が合成する
# checker_evidence.artifact_schema はこの値を使う（Issue #1791）。
READINESS_CHECKER_ARTIFACT_SCHEMA = "ISSUE_CONTRACT_READINESS_RESULT_V1"
REVIEW_ISSUE_CHECKER_ARTIFACT_SCHEMAS = frozenset(
    {
        REVIEW_ISSUE_RESULT_SCHEMA,
        "CHECK_ISSUE_CONTRACT_V1",
        "product_spec_check/v1",
        READINESS_CHECKER_ARTIFACT_SCHEMA,
    }
)
REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER = "deterministic_domain_blocker"
REVIEW_ISSUE_FINDING_KIND_CHECKER_GAP = "checker_gap"
REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN = "heuristic_concern"
VALID_REVIEW_FINDING_KINDS = {
    REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
    REVIEW_ISSUE_FINDING_KIND_CHECKER_GAP,
    REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
}
VALID_REVIEW_ISSUE_STATUS = ("ok", "failed")


def _sha256_prefixed(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _append_findings(
    result: "CheckerResult",
    issues: list[str],
    deterministic_domain_key: str,
    *,
    finding_kind: str,
    blocking: bool,
    checker_evidence: Optional[list[dict]] = None,
    reviewer_blocker_code: Optional[str] = None,
) -> None:
    evidence_entries = list(checker_evidence) if checker_evidence is not None else []

    finding_kind = _validate_finding_kind(finding_kind)
    if finding_kind == REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER and blocking:
        evidence_entries = [
            entry
            for entry in evidence_entries
            if _is_valid_deterministic_evidence(result, entry)
        ]
        if not evidence_entries:
            # Deterministic blocker は current body に結びつく実 evidence が無ければ fail-closed。
            finding_kind = REVIEW_ISSUE_FINDING_KIND_CHECKER_GAP
            blocking = False

    for issue in issues:
        finding = {
            "finding_kind": finding_kind,
            "deterministic_domain_key": deterministic_domain_key,
            "blocking": bool(blocking),
            "checker_evidence": evidence_entries,
            "message": issue,
        }
        result.findings.append(finding)
        if (
            reviewer_blocker_code
            and finding_kind == REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER
            and blocking
        ):
            result.structured_blockers.append(
                {
                    "code": reviewer_blocker_code,
                    "message": issue,
                    "finding_kind": finding_kind,
                    "deterministic_domain_key": deterministic_domain_key,
                    "blocking": True,
                    "checker_evidence": evidence_entries,
                }
            )


def _validate_finding_kind(kind: str) -> str:
    if kind in VALID_REVIEW_FINDING_KINDS:
        return kind
    raise ValueError(f"Unsupported finding_kind: {kind!r}")


def _normalize_readiness_line(value: object) -> Optional[int]:
    """Normalize a readiness error's line_start/line_end value.

    The real `contract_readiness_check.py` producer emits `0` (not omitted,
    not `null`) for validator-timeout / internal-error / JSON-decode-error /
    VC-extraction-failure cases (see `map_validate_errors_to_readiness_errors`
    and the JSON-decode-error branch, both of which default to
    `"line_start": 0`). The consumer schema
    (`schemas/review_issue_result_v1.json` $defs.checker_evidence) requires
    `line_start`/`line_end` to be `null` or an integer `>= 1`. A naive
    `isinstance(value, int)` check lets `0` pass through unchanged and the
    resulting payload is rejected by `jsonschema.validate()` in
    `compact_review_result.py` (`schema_mismatch` — Issue #1791 review
    remediation, Critical #2). `bool` is also excluded because `bool` is an
    `int` subclass in Python.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    return None


def _readiness_error_to_checker_evidence(
    readiness_error: dict,
    *,
    body_sha256: str,
    iteration_id: str,
    artifact_path: str,
) -> dict:
    """Build the `checker_evidence` entry for one readiness errors[] element.

    Preserves the real producer's `source_payload` (decision / classification
    / exit_code / command_hash / ...) when present, instead of dropping it
    (Issue #1791 review remediation, High #5). `checker_evidence` allows
    additional properties, so this does not require a schema change.
    """
    rule_id = str(readiness_error.get("rule_id") or "READINESS_UNKNOWN")
    category = str(readiness_error.get("category") or "contract_readiness")
    source_check = str(
        readiness_error.get("source_check") or "contract_readiness_check"
    )
    evidence_entry = {
        "source_check": source_check,
        "rule_id": rule_id,
        "category": category,
        "artifact_path": artifact_path,
        "artifact_schema": READINESS_CHECKER_ARTIFACT_SCHEMA,
        "body_sha256": body_sha256,
        "iteration_id": iteration_id,
        "line_start": _normalize_readiness_line(readiness_error.get("line_start")),
        "line_end": _normalize_readiness_line(readiness_error.get("line_end")),
    }
    source_payload = readiness_error.get("source_payload")
    if isinstance(source_payload, dict):
        evidence_entry["source_payload"] = source_payload
    return evidence_entry


def readiness_error_to_structured_blocker(
    readiness_error: dict,
    *,
    body_sha256: str,
    iteration_id: str,
    artifact_path: str = "contract_readiness_check_result",
) -> Optional[dict]:
    """Convert one ISSUE_CONTRACT_READINESS_RESULT_V1 errors[] element into the
    REVIEW_ISSUE_RESULT_V1.structured_blocker shape required by
    compact_review_result.py (Issue #1791).

    `contract_readiness_check.py --mode execute` の errors[] は
    rule_id / severity / source_check / category / section / line_start /
    line_end / minimal_context / fix_hint / autofixable という形状であり、
    `code` / `finding_kind` / `deterministic_domain_key` / `blocking` /
    `checker_evidence` を必須とする structured_blocker スキーマとは非互換。
    本関数は readiness error を**そのまま転写せず**、finding 構築ヘルパー
    （`_append_findings` と同じ finding_kind / checker_evidence 形状規約）を
    再利用して変換後の structured_blocker 形状を組み立てる。

    Provenance is validated via the existing `_is_valid_deterministic_evidence()`
    gate (same fail-closed rule `_append_findings()` applies elsewhere in this
    module): if the produced `checker_evidence` entry does not carry a
    non-empty `source_check` / `rule_id` / `category` / `artifact_path` /
    `artifact_schema` (one of `REVIEW_ISSUE_CHECKER_ARTIFACT_SCHEMAS`) /
    `body_sha256` / `iteration_id`, or if `body_sha256` does not match the
    caller-provided `body_sha256`, this function returns `None` instead of a
    fabricated deterministic blocker (Issue #1791 review remediation, High #5).
    Callers (`readiness_errors_to_structured_blockers()` /
    `merge_readiness_into_review_result()`) must drop `None` entries rather
    than emit an invalid structured_blocker.

    `readiness_status == "human_judgment"` errors are never converted to
    deterministic structured_blockers at all — see
    `readiness_errors_to_structured_blockers()` for the routing rule (Issue
    #1791 review remediation, Critical #3).
    """
    rule_id = str(readiness_error.get("rule_id") or "READINESS_UNKNOWN")
    category = str(readiness_error.get("category") or "contract_readiness")
    message = str(
        readiness_error.get("fix_hint")
        or readiness_error.get("message")
        or rule_id
    )
    checker_evidence_entry = _readiness_error_to_checker_evidence(
        readiness_error,
        body_sha256=body_sha256,
        iteration_id=iteration_id,
        artifact_path=artifact_path,
    )

    evidence_shim = _EvidenceBodyShaShim(body_sha256=body_sha256)
    if not _is_valid_deterministic_evidence(evidence_shim, checker_evidence_entry):
        return None

    return {
        "code": rule_id,
        "message": message,
        # finding_kind: deterministic_domain_blocker
        "finding_kind": REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
        "deterministic_domain_key": category,
        "blocking": True,
        "checker_evidence": [checker_evidence_entry],
    }


# PR #2370 OWNER review fix_delta (iteration 1, P0): readiness `errors[]`
# categories that are informational/non-blocking only, independently
# maintained (both this string constant and the identically-named/valued
# `contract_readiness_check.py::_NON_BLOCKING_READINESS_ERROR_CATEGORIES`
# constant are dynamically loaded from separate files, so there is no
# static import boundary to share them across -- Issue #2290 "Notes for
# Reviewer" no-new-package rule). Currently just `extension_surface_
# candidate_advisory` (Issue #2339 EXTSURF003 `unclassified_candidate`
# advisory). `merge_readiness_into_review_result()` routes entries in this
# category to `non_blocking_improvements` instead of `structured_blockers`/
# `blocking_issues`/`failure_class` escalation.
#
# Issue #2852: `runtime_assertion_disposition_classification` (the readiness
# `check_runtime_assertion_disposition_classification()` RUNTIMEASSERT003
# carrier) is registered here too. Its readiness `category` and the
# `non_blocking_improvements[].code` it is merged into are the SAME string
# (`_READINESS_ADVISORY_CODE_BY_CATEGORY`), identical to the code
# `run_checks()` emits directly, so a merged review result keeps ONE carrier
# identity instead of a `RUNTIMEASSERT003` / category split.
_RUNTIME_ASSERTION_DISPOSITION_CARRIER_CODE = "runtime_assertion_disposition_classification"
#
# Issue #2961: `extension_surface_issue_time_exemption_applied` (the readiness
# info-severity carrier for an applied comment-only exemption) is the third
# non-blocking category. Like the disposition carrier, its readiness
# `category` and the `non_blocking_improvements[].code` are the SAME string.
_EXTENSION_SURFACE_ISSUE_TIME_EXEMPTION_CARRIER_CODE = (
    "extension_surface_issue_time_exemption_applied"
)
_NON_BLOCKING_READINESS_ERROR_CATEGORIES = {
    "extension_surface_candidate_advisory",
    _RUNTIME_ASSERTION_DISPOSITION_CARRIER_CODE,
    _EXTENSION_SURFACE_ISSUE_TIME_EXEMPTION_CARRIER_CODE,
}
_READINESS_ADVISORY_CODE_BY_CATEGORY = {
    _RUNTIME_ASSERTION_DISPOSITION_CARRIER_CODE: _RUNTIME_ASSERTION_DISPOSITION_CARRIER_CODE,
    _EXTENSION_SURFACE_ISSUE_TIME_EXEMPTION_CARRIER_CODE: _EXTENSION_SURFACE_ISSUE_TIME_EXEMPTION_CARRIER_CODE,
}
# Carrier codes whose readiness-side copy is dropped from a merged result when
# `run_checks()` already emitted a byte-identical entry (one identity, no dupes).
_DEDUPED_CARRIER_CODES = {
    _RUNTIME_ASSERTION_DISPOSITION_CARRIER_CODE,
    _EXTENSION_SURFACE_ISSUE_TIME_EXEMPTION_CARRIER_CODE,
}


def readiness_errors_to_structured_blockers(
    readiness_errors: list[dict],
    *,
    body_sha256: str,
    iteration_id: str,
    readiness_status: Optional[str] = None,
    artifact_path: str = "contract_readiness_check_result",
) -> list[dict]:
    """Convert a full ISSUE_CONTRACT_READINESS_RESULT_V1 errors[] list into
    REVIEW_ISSUE_RESULT_V1.structured_blockers (Issue #1791).

    `readiness_status == "human_judgment"` readiness results (e.g.
    `env_missing_dep` / `timeout` / unknown classification) are NOT
    deterministic, body-editable blockers, so this function returns an empty
    list for them instead of fabricating `deterministic_domain_blocker` /
    `blocking: true` entries (Issue #1791 review remediation, Critical #3).
    Callers that need to route `human_judgment` distinctly must set the
    top-level `REVIEW_ISSUE_RESULT_V1.failure_class` field instead (see
    `readiness_status_to_failure_class()` and
    `merge_readiness_into_review_result()`). This `failure_class` field is
    NOT read by `compact_review_result.py` / `build_compact_v2()` and does
    not influence the Compact V2 wire's two-valued `NEXT_ACTION` (`proceed
    | request_changes`, unchanged since Issue #2054). Instead, the
    root-owned canonical route (`route_canonical_step2_result()` in
    `issue-refinement-loop/scripts/run_root_review_pipeline.py`) reads this
    SAME `merged_review_result.failure_class` directly from the producer's
    own stdout JSON to decide between the ordinary Step 4 rewrite loop and
    the `step_5_operator_intervention_required` route (Issue #2397
    P0-1/P0-2). A prior design iteration had this function's docstring
    describe `compact_review_result.py` as deriving a
    `NEXT_ACTION: human_judgment_required` wire value from `failure_class`;
    that design was proposed and explicitly rejected by the OWNER (Issue
    #2397 anchor comment P0-3) and is not what this codebase implements.

    Entries whose provenance fails `_is_valid_deterministic_evidence()` (e.g.
    `body_sha256` mismatch) are silently dropped rather than emitted as
    fail-open blockers — see `readiness_error_to_structured_blocker()`.
    """
    if readiness_status == "human_judgment":
        return []
    blockers = []
    for error in readiness_errors:
        blocker = readiness_error_to_structured_blocker(
            error,
            body_sha256=body_sha256,
            iteration_id=iteration_id,
            artifact_path=artifact_path,
        )
        if blocker is not None:
            blockers.append(blocker)
    return blockers


def readiness_status_to_failure_class(readiness_status: Optional[str]) -> Optional[str]:
    """Map `ISSUE_CONTRACT_READINESS_RESULT_V1.status` to the top-level
    `REVIEW_ISSUE_RESULT_V1.failure_class` value (Issue #1791 review
    remediation, Critical #3). Returns `None` for `needs_fix` / `go` /
    other statuses.

    This `failure_class` field is NOT read by `compact_review_result.py` /
    `build_compact_v2()` -- the Compact V2 wire's `NEXT_ACTION` stays the
    two-valued `proceed | request_changes` contract from Issue #2054,
    unmodified. Instead, the root-owned canonical route
    (`route_canonical_step2_result()` in `issue-refinement-loop/scripts/
    run_root_review_pipeline.py`) reads this SAME `merged_review_result.
    failure_class` directly from the producer's own stdout JSON to decide
    between the ordinary Step 4 rewrite loop and the
    `step_5_operator_intervention_required` route (Issue #2397 P0-1/P0-2).
    A prior design iteration described this function as feeding a
    `NEXT_ACTION: human_judgment_required` wire value derived from
    `failure_class`; that design was proposed and explicitly rejected by
    the OWNER (Issue #2397 anchor comment P0-3) and is not what this
    codebase implements.
    """
    if readiness_status == "human_judgment":
        return "contract_readiness_human_judgment"
    return None


# ---------------------------------------------------------------------------
# Issue #2897: bounded `timeout_diagnostics` projection (review merge side).
#
# A readiness `human_judgment` result caused by a command-level VC timeout
# does not become a deterministic `structured_blockers` entry (and must not:
# `failure_class` / the operator-only route are unchanged). Without this
# projection its identity collapses to a `blocking_issues` string. This only
# COPIES values the readiness result already holds (which in turn only copies
# the original `baseline_vc_preflight/v1` result item); it never estimates,
# recomputes, or back-derives a budget (e.g. from `duration_ms` or from the
# current history store) and never reads `parsed_vc_commands`.
# ---------------------------------------------------------------------------

TIMEOUT_DIAGNOSTICS_SCHEMA_VERSION = "TIMEOUT_DIAGNOSTICS_V1"
TIMEOUT_DIAGNOSTICS_MAX_OCCURRENCES = 16
# Per-diagnostic bound. This alone does NOT keep the merged result under the
# transport cap (the diagnostic is added to an already large result); the
# whole-result budget below is what is authoritative.
TIMEOUT_DIAGNOSTICS_MAX_SERIALIZED_BYTES = 16 * 1024
# Mirrors `reviewer_transport.STDOUT_CAP` (65,536 bytes): the transport turns a
# child stdout larger than this into a `capture_failure`. The child writer is
# `run_root_review_pipeline._cmd_run_checker_attempt()`'s
# `print(json.dumps(merged))` (default `ensure_ascii=True`, trailing newline,
# utf-8). A pinned test asserts this constant equals the transport's.
TIMEOUT_DIAGNOSTICS_STDOUT_CAP_BYTES = 65_536

TIMEOUT_ATTRIBUTION_ATTRIBUTED = "attributed"
TIMEOUT_ATTRIBUTION_UNKNOWN = "unknown"
TIMEOUT_REASON_BINDING_VERIFIED = "binding_verified"
TIMEOUT_UNKNOWN_REASON_CODES = (
    "plan_digest_missing",
    "plan_digest_mismatch",
    "provenance_missing",
    "execution_key_missing",
    "dedup_binding_invalid",
    "occurrence_index_out_of_range",
)

_TIMEOUT_PROVENANCE_SOURCES = frozenset(
    {"explicit_override", "static_policy", "static_fallback", "history_estimate"}
)
_TIMEOUT_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMEOUT_ESTIMATOR_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_TIMEOUT_MAX_SECONDS = 86_400
_TIMEOUT_MAX_INDEX = 1_000_000
_TIMEOUT_EXECUTION_SOURCES = frozenset({"executed", "dedup_replay"})


def _timeout_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _timeout_index(value: object) -> Optional[int]:
    if _timeout_plain_int(value) and 0 <= value <= _TIMEOUT_MAX_INDEX:  # type: ignore[operator]
        return value  # type: ignore[return-value]
    return None


def _timeout_sha256(value: object) -> Optional[str]:
    if isinstance(value, str) and _TIMEOUT_SHA256_RE.match(value):
        return value
    return None


def _timeout_bounded_provenance(raw: object) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    timeout_seconds = raw.get("timeout_seconds")
    cleanup_tail_seconds = raw.get("cleanup_tail_seconds")
    source = raw.get("source")
    estimator_version = raw.get("estimator_version")
    estimator_input_digest = _timeout_sha256(raw.get("estimator_input_digest"))
    if not (_timeout_plain_int(timeout_seconds) and 0 < timeout_seconds <= _TIMEOUT_MAX_SECONDS):  # type: ignore[operator]
        return None
    if not (_timeout_plain_int(cleanup_tail_seconds) and 0 <= cleanup_tail_seconds <= _TIMEOUT_MAX_SECONDS):  # type: ignore[operator]
        return None
    # Type first: an unhashable (list / dict) value must degrade to `None`
    # (-> `unknown`), not raise `TypeError` from the frozenset membership test.
    if not isinstance(source, str) or source not in _TIMEOUT_PROVENANCE_SOURCES:
        return None
    if not (isinstance(estimator_version, str) and _TIMEOUT_ESTIMATOR_VERSION_RE.match(estimator_version)):
        return None
    if estimator_input_digest is None:
        return None
    return {
        "timeout_seconds": timeout_seconds,
        "cleanup_tail_seconds": cleanup_tail_seconds,
        "source": source,
        "estimator_version": estimator_version,
        "estimator_input_digest": estimator_input_digest,
    }


def _timeout_error_payload(error: dict) -> dict:
    payload = error.get("source_payload")
    return payload if isinstance(payload, dict) else {}


def _timeout_provenance_identity(provenance: Optional[dict]) -> Optional[tuple]:
    """Hashable, ordered identity of a bounded provenance (dedup source /
    replay comparison). `None` stays `None`."""
    if provenance is None:
        return None
    return (
        provenance["timeout_seconds"],
        provenance["cleanup_tail_seconds"],
        provenance["source"],
        provenance["estimator_version"],
        provenance["estimator_input_digest"],
    )


def _timeout_unknown_reason(
    *,
    top_level_digest: Optional[str],
    results_count: Optional[int],
    error_digest: Optional[str],
    occurrence_index: Optional[int],
    provenance: Optional[dict],
    execution_key_hash: Optional[str],
    execution_source: Optional[str],
    dedup_source_index: Optional[int],
    executed_binding_by_index: dict,
) -> Optional[str]:
    """Return the bounded `unknown` reason code, or `None` if the occurrence
    binding is verified. Pure comparison of values already held by the
    readiness result; nothing is recomputed."""
    if top_level_digest is None:
        return "plan_digest_missing"
    if error_digest != top_level_digest:
        return "plan_digest_mismatch"
    if (
        occurrence_index is None
        or results_count is None
        or occurrence_index >= results_count
    ):
        return "occurrence_index_out_of_range"
    if provenance is None:
        return "provenance_missing"
    if execution_key_hash is None:
        return "execution_key_missing"
    if execution_source == "executed":
        if dedup_source_index is not None:
            return "dedup_binding_invalid"
    elif execution_source == "dedup_replay":
        if (
            dedup_source_index is None
            or dedup_source_index >= results_count
            or dedup_source_index >= occurrence_index
            # The source must itself be a binding-verified `executed`
            # occurrence (see `build_timeout_diagnostics()`), with the same
            # execution key AND the same applied-budget provenance: the
            # execution key covers the timeout, and a replay reuses the
            # source's execution under the very same budget.
            or executed_binding_by_index.get(dedup_source_index)
            != (execution_key_hash, _timeout_provenance_identity(provenance))
        ):
            return "dedup_binding_invalid"
    else:
        return "dedup_binding_invalid"
    return None


def build_timeout_diagnostics(
    readiness_result: dict, *, body_sha256: str
) -> Optional[dict]:
    """Project command-level VC timeout readiness errors into a bounded
    `TIMEOUT_DIAGNOSTICS_V1` object (Issue #2897), or `None` if there is no
    timeout occurrence.

    Occurrences are listed in the readiness result's own (canonical result)
    order. `occurrence_index` -- the position in the unfiltered `results`
    array -- is the primary identifier; `line` is block-relative and only
    auxiliary. An occurrence whose binding cannot be verified is recorded as
    `attribution: unknown` with a bounded reason code and carries no
    provenance / execution identity (never completed from another VC or from
    a recomputed budget).
    """
    errors = readiness_result.get("errors") or []
    top_level_digest = _timeout_sha256(readiness_result.get("canonical_plan_digest"))
    raw_results_count = readiness_result.get("results_count")
    results_count = (
        raw_results_count
        if _timeout_plain_int(raw_results_count) and 0 <= raw_results_count <= _TIMEOUT_MAX_INDEX
        else None
    )

    # `executed` result items by canonical index (any category), used to
    # check that a dedup replay points at an actual execution source with the
    # same execution key and the same applied-budget provenance. A source is
    # registered ONLY if its own binding integrity is verified from values
    # already in this readiness result (plan digest, occurrence index range,
    # provenance, execution key): an invalid source must never lend
    # `binding_verified` to a replay. An index that appears twice with
    # conflicting identity is not registered at all.
    executed_binding_by_index: dict = {}
    conflicting_source_indexes: set = set()
    for error in errors:
        if not isinstance(error, dict):
            continue
        payload = _timeout_error_payload(error)
        index = _timeout_index(payload.get("occurrence_index"))
        key = _timeout_sha256(payload.get("execution_key_hash"))
        if index is None or key is None or payload.get("execution_source") != "executed":
            continue
        source_provenance = _timeout_bounded_provenance(payload.get("timeout_provenance"))
        source_reason = _timeout_unknown_reason(
            top_level_digest=top_level_digest,
            results_count=results_count,
            error_digest=_timeout_sha256(payload.get("canonical_plan_digest")),
            occurrence_index=index,
            provenance=source_provenance,
            execution_key_hash=key,
            execution_source="executed",
            dedup_source_index=_timeout_index(payload.get("dedup_source_result_index")),
            executed_binding_by_index={},
        )
        if source_reason is not None:
            continue
        identity = (key, _timeout_provenance_identity(source_provenance))
        if index in conflicting_source_indexes:
            continue
        if executed_binding_by_index.get(index, identity) != identity:
            del executed_binding_by_index[index]
            conflicting_source_indexes.add(index)
            continue
        executed_binding_by_index[index] = identity

    timeout_errors = [
        error
        for error in errors
        if isinstance(error, dict)
        and error.get("category") == "timeout"
        and error.get("source_check") == "baseline_vc_preflight"
    ]
    if not timeout_errors:
        return None

    occurrences: list[dict] = []
    for error in timeout_errors:
        payload = _timeout_error_payload(error)
        occurrence_index = _timeout_index(payload.get("occurrence_index"))
        line = _timeout_index(error.get("line_start"))
        command_hash = _timeout_sha256(payload.get("command_hash"))
        execution_key_hash = _timeout_sha256(payload.get("execution_key_hash"))
        execution_source = payload.get("execution_source")
        # Type first (an unhashable list / dict must not raise `TypeError`).
        if not isinstance(execution_source, str) or execution_source not in _TIMEOUT_EXECUTION_SOURCES:
            execution_source = None
        dedup_source_index = _timeout_index(payload.get("dedup_source_result_index"))
        provenance = _timeout_bounded_provenance(payload.get("timeout_provenance"))

        reason = _timeout_unknown_reason(
            top_level_digest=top_level_digest,
            results_count=results_count,
            error_digest=_timeout_sha256(payload.get("canonical_plan_digest")),
            occurrence_index=occurrence_index,
            provenance=provenance,
            execution_key_hash=execution_key_hash,
            execution_source=execution_source,
            dedup_source_index=dedup_source_index,
            executed_binding_by_index=executed_binding_by_index,
        )
        if reason is None:
            occurrences.append(
                {
                    "attribution": TIMEOUT_ATTRIBUTION_ATTRIBUTED,
                    "reason_code": TIMEOUT_REASON_BINDING_VERIFIED,
                    "occurrence_index": occurrence_index,
                    "line": line,
                    "line_coordinate": "block_relative",
                    "command_hash": command_hash,
                    "execution_key_hash": execution_key_hash,
                    "execution_source": execution_source,
                    "dedup_source_result_index": dedup_source_index,
                    "timeout_provenance": provenance,
                }
            )
        else:
            occurrences.append(
                {
                    "attribution": TIMEOUT_ATTRIBUTION_UNKNOWN,
                    "reason_code": reason,
                    "occurrence_index": None,
                    "line": line,
                    "line_coordinate": "block_relative",
                    "command_hash": command_hash,
                    "execution_key_hash": None,
                    "execution_source": None,
                    "dedup_source_result_index": None,
                    "timeout_provenance": None,
                }
            )

    total = len(occurrences)
    kept = occurrences[:TIMEOUT_DIAGNOSTICS_MAX_OCCURRENCES]
    diagnostics = {
        "schema_version": TIMEOUT_DIAGNOSTICS_SCHEMA_VERSION,
        "body_sha256": body_sha256,
        "canonical_plan_digest": top_level_digest,
        "results_count": results_count,
        "total_timeout_occurrences": total,
        "truncated_count": total - len(kept),
        "occurrences": kept,
    }
    # Structural fields are already bounded; this is a last-resort guard so
    # the serialized size limit can never be exceeded.
    while kept and len(json.dumps(diagnostics, ensure_ascii=True).encode("utf-8")) > (
        TIMEOUT_DIAGNOSTICS_MAX_SERIALIZED_BYTES
    ):
        kept.pop()
        diagnostics["truncated_count"] = total - len(kept)
    return diagnostics


def _stdout_bytes_of_review_result(result: dict) -> int:
    """Byte size of `result` as the root review child actually writes it:
    `print(json.dumps(merged))` (default options, trailing newline, utf-8)."""
    return len((json.dumps(result) + "\n").encode("utf-8"))


def fit_timeout_diagnostics_to_stdout_budget(
    base_result: dict, diagnostics: dict
) -> Optional[dict]:
    """Shrink the OPTIONAL `timeout_diagnostics` so the WHOLE merged result
    (`base_result` + the diagnostic) stays within the transport stdout cap.

    The per-diagnostic 16 KiB bound is not sufficient: the diagnostic is added
    to an already large result, and a stdout above
    `TIMEOUT_DIAGNOSTICS_STDOUT_CAP_BYTES` becomes a transport
    `capture_failure` (a retry-eligible loss of the whole review result).
    Only occurrences are dropped (and `truncated_count` kept consistent with
    `total_timeout_occurrences`); `base_result` -- verdict, failure_class,
    blockers -- is never touched, and JSON is never cut mid-stream. If even
    the header-only diagnostic does not fit, `None` is returned and the
    caller omits the optional field (the pre-existing contract shape).

    `base_result` must already be in its final form (every other mutation
    applied) so the measured size is the size actually written.
    """
    if "timeout_diagnostics" in base_result:
        base_result = {k: v for k, v in base_result.items() if k != "timeout_diagnostics"}
    total = diagnostics["total_timeout_occurrences"]
    all_occurrences = list(diagnostics["occurrences"])
    for keep in range(len(all_occurrences), -1, -1):
        candidate = dict(diagnostics)
        candidate["occurrences"] = all_occurrences[:keep]
        candidate["truncated_count"] = total - keep
        trial = dict(base_result)
        trial["timeout_diagnostics"] = candidate
        if _stdout_bytes_of_review_result(trial) <= TIMEOUT_DIAGNOSTICS_STDOUT_CAP_BYTES:
            return candidate
    return None


def merge_readiness_into_review_result(
    review_result: dict,
    readiness_result: dict,
    *,
    readiness_artifact_path: str,
    iteration_id: str,
) -> dict:
    """Deterministically merge ISSUE_CONTRACT_READINESS_RESULT_V1 (producer:
    `contract_readiness_check.py --mode execute`) into REVIEW_ISSUE_RESULT_V1.

    This is the CLI-wired replacement for the previously prompt-driven merge
    described in `SKILL.md` Step 2 (Issue #1791 review remediation, Critical
    #1): no LLM call sits between the two producers and
    `compact_review_result.py`. Invoked via
    `check_issue_contract.py --mode merge_readiness`.

    Fail-closed: if `readiness_result.errors` is non-empty but
    `review_result.body_sha256` does not match `readiness_result.body_sha256`,
    this raises `ValueError` rather than emitting deterministic blockers built
    on stale/mismatched evidence (Issue #1791 review remediation, High #5).
    Individual errors whose evidence is otherwise invalid are dropped by
    `readiness_errors_to_structured_blockers()`.

    PR #2370 OWNER review fix_delta (iteration 1, P0): readiness errors whose
    `category` is in `_NON_BLOCKING_READINESS_ERROR_CATEGORIES` (currently
    only `extension_surface_candidate_advisory`, Issue #2339's EXTSURF003
    `unclassified_candidate` advisory) are non-blocking by construction --
    they must never be converted into a `structured_blockers` entry, never
    counted in `blocking_issues`, and must never cause `verdict` /
    `failure_class` escalation. Previously this function blindly converted
    every entry in `readiness_result.errors` (blocking or not) into a
    blocking `structured_blocker`, which silently turned an
    advisory-only-classified Allowed Path (e.g.
    `.claude/rules/project-constitution.md`) into a `needs-fix` verdict --
    a direct violation of Issue #2339 AC6. Such advisory entries are instead
    routed into `non_blocking_improvements`.
    """
    merged = json.loads(json.dumps(review_result))
    timeout_diagnostics: Optional[dict] = None
    review_body_sha256 = merged.get("body_sha256")
    readiness_body_sha256 = readiness_result.get("body_sha256")
    readiness_errors = readiness_result.get("errors") or []
    readiness_status = readiness_result.get("status")

    if readiness_errors:
        if not review_body_sha256 or review_body_sha256 != readiness_body_sha256:
            raise ValueError(
                "body_sha256 mismatch between REVIEW_ISSUE_RESULT_V1 "
                f"({review_body_sha256!r}) and "
                "ISSUE_CONTRACT_READINESS_RESULT_V1 "
                f"({readiness_body_sha256!r}); refusing to merge (fail-closed)"
            )

        blocking_readiness_errors = [
            error
            for error in readiness_errors
            if error.get("category") not in _NON_BLOCKING_READINESS_ERROR_CATEGORIES
        ]
        advisory_readiness_errors = [
            error
            for error in readiness_errors
            if error.get("category") in _NON_BLOCKING_READINESS_ERROR_CATEGORIES
        ]

        if advisory_readiness_errors:
            existing_improvements = list(merged.get("non_blocking_improvements") or [])
            new_improvements = [
                {
                    "code": str(
                        _READINESS_ADVISORY_CODE_BY_CATEGORY.get(error.get("category"))
                        or error.get("rule_id")
                        or "READINESS_ADVISORY"
                    ),
                    "severity": "advisory",
                    "evidence": error.get("minimal_context", []),
                    "suggested_action": str(
                        error.get("fix_hint") or error.get("category") or "readiness advisory"
                    ),
                }
                for error in advisory_readiness_errors
            ]
            # Issue #2852: `run_checks()` already emits the disposition
            # carrier directly into the review result, and the readiness
            # result carries the same classification (same shared evaluator,
            # same evidence lines). A merged result must expose it ONCE:
            # skip a readiness-side carrier whose code AND evidence are
            # byte-identical to an entry already present. Deliberately
            # narrow: carrier code only, exact equality only (a differing
            # classification stays visible as two entries rather than being
            # silently collapsed). Not a general advisory dedupe.
            new_improvements = [
                entry
                for entry in new_improvements
                if not (
                    entry["code"] in _DEDUPED_CARRIER_CODES
                    and any(
                        existing.get("code") == entry["code"]
                        and existing.get("evidence") == entry["evidence"]
                        for existing in existing_improvements
                    )
                )
            ]
            merged["non_blocking_improvements"] = existing_improvements + new_improvements

        new_blockers: list = []
        if blocking_readiness_errors:
            new_blockers = readiness_errors_to_structured_blockers(
                blocking_readiness_errors,
                body_sha256=review_body_sha256,
                iteration_id=iteration_id,
                readiness_status=readiness_status,
                artifact_path=readiness_artifact_path,
            )
            merged["structured_blockers"] = (
                list(merged.get("structured_blockers") or []) + new_blockers
            )
            merged["blocking_issues"] = list(merged.get("blocking_issues") or []) + [
                str(
                    error.get("fix_hint")
                    or error.get("message")
                    or error.get("rule_id")
                    or "readiness_error"
                )
                for error in blocking_readiness_errors
            ]

            failure_class = readiness_status_to_failure_class(readiness_status)
            if failure_class:
                merged["failure_class"] = failure_class
                # Issue #2897: bounded timeout occurrence / applied-budget
                # identity. Additive optional field only -- no blocker, no
                # failure_class / route change. Reached only after the
                # body_sha256 fail-closed check above, so a mismatched
                # readiness result never produces a diagnostic.
                # Attached only after every other mutation below, and fitted
                # to the whole-result stdout budget (see
                # `fit_timeout_diagnostics_to_stdout_budget()`).
                timeout_diagnostics = build_timeout_diagnostics(
                    readiness_result, body_sha256=review_body_sha256
                )
                # `compact_review_result.py` checks `verdict == "approve"` first
                # and short-circuits to `NEXT_ACTION: proceed` before it ever
                # looks at `failure_class` (Issue #1791 review remediation
                # iteration 3). If the underlying contract check itself
                # returned `verdict: approve` (readiness errors such as
                # env_missing_dep/timeout are independent of body content and
                # can coexist with an approve verdict), force it to
                # `needs-fix` here so the human_judgment failure_class is not
                # silently dropped by compact_review_result.py's
                # verdict-first branching.
                if merged.get("verdict") == "approve":
                    merged["verdict"] = "needs-fix"
            elif new_blockers:
                merged["verdict"] = "needs-fix"

    if timeout_diagnostics is not None:
        fitted = fit_timeout_diagnostics_to_stdout_budget(merged, timeout_diagnostics)
        if fitted is not None:
            merged["timeout_diagnostics"] = fitted

    _validate_review_issue_result_payload(merged)
    return merged


def _validate_status(status: str) -> str:
    if status in VALID_REVIEW_ISSUE_STATUS:
        return status
    raise ValueError(f"Unsupported REVIEW_ISSUE_RESULT status: {status!r}")


@dataclass
class _EvidenceBodyShaShim:
    """Minimal duck-typed stand-in for `CheckerResult` used to reuse
    `_is_valid_deterministic_evidence()`'s fail-closed evidence gate from
    contexts (e.g. `readiness_error_to_structured_blocker()`) that only need
    the `body_sha256` comparison and do not have a full `CheckerResult`
    instance available (Issue #1791 review remediation, High #5)."""

    body_sha256: str


def _is_valid_deterministic_evidence(result: "CheckerResult", entry: dict) -> bool:
    required_fields = (
        "source_check",
        "rule_id",
        "category",
        "artifact_path",
        "artifact_schema",
        "body_sha256",
        "iteration_id",
    )
    for field_name in required_fields:
        value = entry.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return False
    if entry.get("artifact_schema") not in REVIEW_ISSUE_CHECKER_ARTIFACT_SCHEMAS:
        return False
    if entry.get("body_sha256") != result.body_sha256:
        return False
    return True


def _load_review_issue_result_schema() -> dict:
    return json.loads(REVIEW_ISSUE_RESULT_SCHEMA_FILE.read_text(encoding="utf-8"))


def _validate_review_issue_result_payload(payload: dict) -> None:
    import jsonschema

    jsonschema.validate(instance=payload, schema=_load_review_issue_result_schema())


@dataclass
class ParsedVcCommand:
    """A VC command parsed from the Verification Commands section, with optional annotation metadata.

    Fields:
        command: the raw command string (e.g. "$ rg -n foo bar.py")
        preflight_scope: PreflightScope value if # preflight-scope: annotation was directly above;
                         None if no annotation present.
        trivially_pass_reason: non-empty reason string if # trivially_pass: annotation was directly
                                above the command; None otherwise.
        classification: "executable" | "skipped"
        skip_reason_type: "preflight_scope" | "trivially_pass" | None (only set when skipped)
    """
    command: str
    preflight_scope: Optional[PreflightScope] = None
    trivially_pass_reason: Optional[str] = None
    classification: str = "executable"
    skip_reason_type: Optional[str] = None


def parse_vc_commands(vc_section: str) -> list[ParsedVcCommand]:
    """Parse VC commands from a Verification Commands section with annotation support.

    Rules:
    - Commands are lines starting with "$" inside ```bash code blocks.
    - A command directly preceded (no blank lines or non-annotation comments between) by:
        # preflight-scope: <value>  → sets preflight_scope, classification: skipped
        # trivially_pass: <reason>  → sets trivially_pass_reason, classification: skipped
    - Annotation comments themselves are NOT extracted as commands.
    - If a blank line or a non-annotation comment appears between annotation and command,
      the annotation is invalidated (annotation must be immediately above the command).
    - unknown preflight-scope values → PreflightScope.UNKNOWN, classification: skipped,
      skip_reason_type: "preflight_scope_human_judgment"

    Returns a list of ParsedVcCommand, one per extracted command.
    """
    results: list[ParsedVcCommand] = []

    # Extract all code blocks (bash or untyped)
    code_blocks = re.findall(r'```[^\n]*\n(.*?)```', vc_section, re.DOTALL)

    _preflight_scope_re = re.compile(r'^#\s*preflight-scope:\s*(.+)$')
    _trivially_pass_re = re.compile(r'^#\s*trivially_pass:\s*(.+)$')
    _annotation_re = re.compile(r'^#\s*(preflight-scope|trivially_pass):')

    for block in code_blocks:
        lines = block.splitlines()
        # State: pending annotation for the next command line
        pending_preflight_scope: Optional[str] = None
        pending_trivially_pass: Optional[str] = None
        # Track whether last non-blank line was an annotation (for invalidation)
        _last_was_annotation = False

        for line in lines:
            stripped = line.strip()

            if not stripped:
                # Blank line: invalidate pending annotations
                pending_preflight_scope = None
                pending_trivially_pass = None
                _last_was_annotation = False
                continue

            ps_match = _preflight_scope_re.match(stripped)
            tp_match = _trivially_pass_re.match(stripped)

            if ps_match:
                # This line is a # preflight-scope: annotation — do not emit as command
                # Invalidate any previously pending annotation (only last one counts)
                pending_preflight_scope = ps_match.group(1).strip()
                pending_trivially_pass = None  # reset other annotation
                _last_was_annotation = True
                continue

            if tp_match:
                # This line is a # trivially_pass: annotation — do not emit as command
                pending_trivially_pass = tp_match.group(1).strip()
                pending_preflight_scope = None  # reset other annotation
                _last_was_annotation = True
                continue

            # Non-annotation comment line: invalidate pending annotations (AC6)
            if stripped.startswith('#') and not _annotation_re.match(stripped):
                pending_preflight_scope = None
                pending_trivially_pass = None
                _last_was_annotation = False
                continue

            # Command line: must start with "$" to be considered a VC command
            if stripped.startswith('$'):
                cmd = ParsedVcCommand(command=stripped)

                if pending_preflight_scope is not None:
                    scope_val = pending_preflight_scope
                    if scope_val == PreflightScope.PR_REVIEW_ONLY.value:
                        cmd.preflight_scope = PreflightScope.PR_REVIEW_ONLY
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "preflight_scope"
                    elif scope_val == PreflightScope.RUNTIME_ONLY.value:
                        cmd.preflight_scope = PreflightScope.RUNTIME_ONLY
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "preflight_scope"
                    else:
                        # unknown value: fail-closed as human_judgment
                        cmd.preflight_scope = PreflightScope.UNKNOWN
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "preflight_scope_human_judgment"

                elif pending_trivially_pass is not None:
                    reason = pending_trivially_pass
                    if reason:
                        cmd.trivially_pass_reason = reason
                        cmd.classification = "skipped"
                        cmd.skip_reason_type = "trivially_pass"

                results.append(cmd)
                # Reset pending annotations after consuming
                pending_preflight_scope = None
                pending_trivially_pass = None
                _last_was_annotation = False
            else:
                # Non-command, non-annotation, non-blank line: invalidate annotations
                # (e.g. a comment like "# some other remark" — already handled above,
                # but also handles output lines etc.)
                pending_preflight_scope = None
                pending_trivially_pass = None
                _last_was_annotation = False

    return results


# --- scope_cvs_in_scope_mismatch tokenization constants (Issue #396) ---

PATH_TOKEN_EXTENSIONS = (".md", ".py", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".toml", ".sh")
PATH_TOKEN_PREFIXES = ("docs/", ".claude/", ".github/")
PATH_TOKEN_STRIP_CHARS = ".,:;)]}>"

# SSOT: PATH_TOKEN_RE auto-generated from PATH_TOKEN_EXTENSIONS and PATH_TOKEN_PREFIXES (Blocker 4 fix)
_EXT_RE = "|".join(re.escape(ext.lstrip(".")) for ext in PATH_TOKEN_EXTENSIONS)
_PREFIX_RE = "|".join(re.escape(prefix.rstrip("/")) for prefix in PATH_TOKEN_PREFIXES)

# ASCII-only path components, Unicode path matching is out of scope (Non-blocking C fix).
# Blocker 3: trailing sentence-final punctuation (.,;) is included in the match then stripped via
# rstrip(PATH_TOKEN_STRIP_CHARS). Both extension branch and prefix branch allow optional trailing
# punctuation chars so that "src/foo.py." and "config/settings.yaml." are captured and normalized.
# The suffix group [.,;]? must be kept outside PATH_TOKEN_STRIP_CHARS rstrip so we only need
# the lookahead to handle the character AFTER the optional trailing punct.
_SENT_PUNCT = r"[.,;]?"  # optional sentence-final punctuation included in match; rstripped later
_PATH_BOUNDARY_END = r"(?=$|[\s:)\]}>\"。．、])"  # must NOT include . , ; (already in _SENT_PUNCT)

PATH_TOKEN_RE = re.compile(
    r"(?<![/A-Za-z0-9_.~-])"
    + r"(?:"
    + r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:" + _EXT_RE + r")" + _SENT_PUNCT
    + r"|"
    + r"(?:" + _PREFIX_RE + r")/(?:[A-Za-z0-9_./-]+)" + _SENT_PUNCT
    + r")"
    + _PATH_BOUNDARY_END
)

# Bullet pattern: matches "- ", "* ", "+ " and indented variants (up to 3 spaces) (Blocker 2 fix)
# NOTE: This is the shared scope-tokenization bullet pattern (Issue #396 / PR #430),
# used by _bullet_tokens() below. It is intentionally general (any indent 0-3, any
# whitespace after marker) because it only needs to find candidate bullet lines for
# token extraction. C2 Stop Conditions counting has stricter, independent semantics
# (fence exclusion / top-level-only / thematic-break exclusion) and MUST NOT reuse
# this pattern directly — see iter_top_level_stop_condition_items() (Issue #1431).
BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")

# ---------------------------------------------------------------------------
# C2 Stop Conditions dedicated tokenizer (Issue #1431 review remediation).
# ---------------------------------------------------------------------------
# GFM thematic break: 0-3 leading spaces, then 3+ repetitions of the same
# marker char (*, -, _) optionally interleaved with spaces/tabs, and nothing
# else on the line. These lines superficially resemble a bullet marker line
# (e.g. "* * *", "- - -") but are NOT list items and must not be counted.
_THEMATIC_BREAK_RE = re.compile(
    r"^ {0,3}(?:\*[ \t]*){3,}$"
    r"|^ {0,3}(?:-[ \t]*){3,}$"
    r"|^ {0,3}(?:_[ \t]*){3,}$"
)

# Stop-Conditions bullet line: allowed markers are '-', '*', '+' only.
# Group 1: leading indentation, ASCII space characters ONLY (a leading tab
#   before the marker means the whole line fails to match this pattern —
#   tab-indented lines are rejected outright, never counted as top-level or
#   nested).
# Group 2: the marker character.
# Group 3: one or more literal ASCII space (0x20) characters after the
#   marker. A tab immediately after the marker does NOT satisfy this group,
#   so such a line does not match (rejected, not silently accepted). This
#   avoids relying on Python's `\s` Unicode whitespace semantics.
# Group 4: the remaining line content (may be empty/whitespace-only; that is
#   checked separately so empty items are excluded).
_STOP_CONDITION_BULLET_LINE_RE = re.compile(r"^( *)([-*+])( +)(.*)$")


def iter_top_level_stop_condition_items(section: str) -> Iterator[str]:
    """Yield the stripped content of each top-level Stop Conditions bullet item.

    C2-dedicated tokenizer (Issue #1431 review remediation): counting Stop
    Conditions items has different semantics than the shared scope
    tokenization BULLET_RE (see the comment above BULLET_RE), so this
    function is deliberately independent and fixes the following behaviors:

      - Code fence content (``` ... ```) is excluded entirely, using the
        same iter_markdown_blocks() SSOT as extract_section().
      - Only TOP-LEVEL items are yielded. A bullet line's leading space
        count must be less than or equal to the leading space count of the
        first recognized bullet line encountered in the section; deeper
        bullet lines are treated as nested children of the preceding item
        and are excluded (previously, any bullet indented 0-3 spaces was
        counted as top-level regardless of nesting).
      - GFM thematic break lines ("* * *", "- - -", "___", etc.) are
        excluded even though they superficially match the marker pattern.
      - Items whose content is empty or whitespace-only are excluded.
      - Allowed markers are '-', '*', '+' only; the marker must be followed
        by one or more ASCII space characters (tabs after the marker, or a
        leading tab before the marker, are rejected — not counted at all).
    """
    lines = section.splitlines(keepends=True)

    # fence 内行の除外（extract_section と同じ SSOT: iter_markdown_blocks）
    fence_line_indices: set[int] = set()
    current_line_idx = 0
    for block_text, block_kind in _iter_markdown_blocks(section):
        block_lines = block_text.splitlines(keepends=True)
        if block_kind == _BLOCK_KIND_CODE_FENCE:
            for k in range(len(block_lines)):
                fence_line_indices.add(current_line_idx + k)
        current_line_idx += len(block_lines)

    top_level_indent: "int | None" = None

    for idx, raw_line in enumerate(lines):
        if idx in fence_line_indices:
            continue

        line = raw_line.rstrip("\n")

        if _THEMATIC_BREAK_RE.match(line):
            continue

        m = _STOP_CONDITION_BULLET_LINE_RE.match(line)
        if not m:
            continue

        content = m.group(4).strip()
        if not content:
            continue

        indent = len(m.group(1))
        if top_level_indent is None:
            top_level_indent = indent

        if indent > top_level_indent:
            # Deeper-indented bullet: nested child of the preceding item.
            continue

        # Sibling at (or shallower than) the established top-level indent.
        top_level_indent = indent
        yield content

SIGNIFICANT_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{4,}")

STOP_TOKENS: frozenset[str] = frozenset({
    "issue", "scope", "current", "validated", "warning",
    "token", "tokens", "checker", "review", "script",
    "scripts", "test", "tests", "fixture", "fixtures",
    "implementation", "function", "detect", "result",
    "output", "outcome", "verification", "commands",
    "acceptance", "criteria", "allowed", "paths",
    "this", "that", "with", "from", "will", "also",
    "have", "been", "more", "than", "when", "then",
})

JACCARD_THRESHOLD = 0.3

# --- end of scope_cvs tokenization constants ---

WORKFLOW_SKILLS = {
    "implement-issue",
    "pr-review-judge",
    "ssot-discovery",
    "issue-refinement-loop",
    "impl-review-loop",
    "issue-contract-review",
    "open-pr",
    "post-merge-cleanup",
    "edit-issue",
    "create-issue",
}

SUBJECTIVE_PATTERNS = [
    r"適切に動作",
    r"品質を改善",
    r"最適化",
    r"よりよい",
    r"より良い",
    r"適切な",
    r"良好な",
    r"効果的に",
    r"efficiently",
    r"appropriately",
    r"properly",
    r"\boptimized?\b",
    r"\bimproved?\b",
]

VAGUE_OUTCOME_PATTERNS = [
    r"〜が決定される",
    r"〜を検討する",
    r"〜を改善する",
    r"が決定される",
    r"を検討する",
    r"を改善する",
    r"検討する",
    r"決定される",
]

IMPLEMENTATION_REQUIRED_SECTIONS = [
    "Outcome",
    "Acceptance Criteria",
    "Verification Commands",
    "Stop Conditions",
    "Runtime Verification Applicability",
    "Allowed Paths",
]


def get_required_sections(issue_kind: str, template_path: str = ".github/ISSUE_TEMPLATE/implementation.yml") -> list:
    """Issue template から必須セクションを動的取得。未存在時はハードコードにフォールバック。"""
    if issue_kind != "implementation":
        return []

    import os
    import yaml as _yaml
    if os.path.exists(template_path):
        try:
            with open(template_path) as f:
                tmpl = _yaml.safe_load(f)
            required = [
                item["attributes"]["label"]
                for item in tmpl.get("body", [])
                if item.get("type") != "markdown"
                   and item.get("validations", {}).get("required", False)
                   and "label" in item.get("attributes", {})
            ]
            if required:
                return required
        except Exception:
            pass

    # fallback
    return IMPLEMENTATION_REQUIRED_SECTIONS


@dataclass
class DeterministicChecks:
    C1_required_sections: str = CheckResult.NA
    C2_stop_conditions_6: str = CheckResult.NA
    C3_ac_checkbox_format: str = CheckResult.NA
    C4_vc_commands_present: str = CheckResult.NA
    C5_ac_vc_number_alignment: str = CheckResult.NA
    C6_no_subjective_phrasing: str = CheckResult.NA
    C7_required_skills_semantics: str = CheckResult.NA
    C8_outcome_concreteness: str = CheckResult.NA
    C9_runtime_applicability_present: str = CheckResult.NA
    C10_deferred_destination_present: str = CheckResult.NA
    C11_decision_tag_consistency: str = CheckResult.NA
    C12_product_trace_fields_structure: str = CheckResult.NA
    C13_vc_preflight_decision_consistency: str = CheckResult.NA
    C14_extension_surface_risk_trigger: str = CheckResult.NA
    C15_runtime_assertion_binding_coverage: str = CheckResult.NA


@dataclass
class CheckerResult:
    schema: str = REVIEW_ISSUE_RESULT_SCHEMA
    schema_version: str = REVIEW_ISSUE_RESULT_SCHEMA_VERSION
    status: str = "ok"
    body_sha256: str = ""
    verdict: str = "approve"
    deterministic_checks: DeterministicChecks = field(default_factory=DeterministicChecks)
    blocking_issues: list[str] = field(default_factory=list)
    structured_blockers: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    # list of dict {code, severity, evidence, suggested_action}
    non_blocking_improvements: list = field(default_factory=list)
    diff_proposal: dict = field(default_factory=lambda: {"add": [], "remove": [], "rewrite": []})
    issue_kind: str = "implementation"
    parsed_vc_commands: list = field(default_factory=list)  # list[ParsedVcCommand] (serialized dicts)


PLACEHOLDER_VALUES = {"", "tbd", "todo", "none", "n/a", "na", "<tbd>", "<todo>"}
PLACEHOLDER_PATTERN = re.compile(r"^\s*<[^>]+>\s*$")  # <...> 形式
REQUIREMENT_ID_PATTERN = re.compile(r"^REQ-\d{3,}$")
SOURCE_TASK_ID_PATTERN = re.compile(r"^(T|TASK-)\d{3,}$")


def _add_warning(
    result: "CheckerResult",
    code: str,
    severity: str,
    evidence: list,
    suggested_action: str,
    *,
    emit_finding: bool = True,
) -> None:
    """Append a structured non_blocking_improvement entry."""
    result.non_blocking_improvements.append({
        "code": code,
        "severity": severity,
        "evidence": evidence,
        "suggested_action": suggested_action,
    })
    if emit_finding:
        result.findings.append(
            {
                "finding_kind": REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
                "deterministic_domain_key": code,
                "blocking": False,
                "checker_evidence": [],
                "message": suggested_action,
            }
        )


def extract_section(body: str, section_name: str) -> str:
    """Extract text under a ## section heading until the next ## heading.

    heading_policy (#654 B2): bilingual heading（例: ## 成果物 (Outcome)）も
    canonical_en（"Outcome"）として認識する。
    GFM ATX heading 仕様（0-3 spaces indent / 任意 closing #）に対応した
    parse_atx_heading_line() を使って各行を解析し、heading_policy の
    lookup_heading_policy() で canonical_en を照合する（SSOT 共用）。

    fence 対応 (#654 iter3): iter_markdown_blocks() で fence ブロックを識別し、
    code fence 内の行を見出し境界判定から除外する（SSOT: prose_boundary_policy）。
    境界は旧実装同様 level-2（##）見出しに限定する（level-1 等を境界扱いしない）。
    """
    lines = body.splitlines(keepends=True)
    n = len(lines)

    # fence 内行の行番号セットを構築（iter_markdown_blocks SSOT 利用）
    fence_line_indices: set[int] = set()
    current_line_idx = 0
    for block_text, block_kind in _iter_markdown_blocks(body):
        block_lines = block_text.splitlines(keepends=True)
        if block_kind == _BLOCK_KIND_CODE_FENCE:
            for k in range(len(block_lines)):
                fence_line_indices.add(current_line_idx + k)
        current_line_idx += len(block_lines)

    # 各行を走査して section_name に対応する level-2 見出しを探す
    for i, line in enumerate(lines):
        # fence 内行はスキップ（見出し境界判定から除外）
        if i in fence_line_indices:
            continue

        # GFM ATX heading として解析（B2: leading spaces / closing # 対応）
        parsed = _parse_atx_heading_line(line.rstrip('\n'))
        if parsed is None:
            continue

        # 境界は level-2（##）のみ（旧実装の ^## セマンティクスを維持）
        if parsed['level'] != 2:
            continue

        heading_text = parsed['text']

        # セクション名と照合: exact match (英語正規見出し) + heading_policy（bilingual）
        matched = False
        if heading_text == section_name:
            matched = True
        elif _HEADING_POLICY_AVAILABLE:
            policy = _lookup_heading_policy(heading_text)
            if policy and policy.get("canonical_en") == section_name:
                matched = True

        if not matched:
            continue

        # 見出しの次の行から次の level-2 heading（または EOF）までを収集
        # fence 内行も収集対象（セクション本文の一部）だが、境界判定には使わない
        start = i + 1
        end = n
        for j in range(start, n):
            # fence 内行は境界判定をスキップ
            if j in fence_line_indices:
                continue
            # 次の level-2 heading を GFM ATX parser で検出
            nxt = _parse_atx_heading_line(lines[j].rstrip('\n'))
            if nxt is not None and nxt['level'] == 2:
                end = j
                break

        section_body = ''.join(lines[start:end])
        return section_body.strip()

    return ""


def detect_issue_kind(body: str, labels: str = "", title: str = "") -> str:
    """Issue kind を検出する。Machine-Readable Contract を最優先で参照。

    SSOT: docs/dev/github-ops.md ## ISSUE_KIND_POLICY_V1

    - canonical_kinds（implementation / research / parent）はそのまま返す。
    - aliases（design → research, tracking → parent）は正規化して返す。
    - allowlist にも aliases にも存在しない未知の kind は UNKNOWN_ISSUE_KIND_SENTINEL を返す
      （silent "implementation" fallback は禁止）。
    - SSOT が読み込めない場合（IssueKindPolicyLoadError）は UNKNOWN_ISSUE_KIND_SENTINEL を返す
      （SSOT 不在を "implementation" に誤解させない）。
    """
    try:
        policy = _load_issue_kind_policy()
    except IssueKindPolicyLoadError:
        return UNKNOWN_ISSUE_KIND_SENTINEL

    canonical_kinds = policy["canonical_kinds"]
    aliases = policy["aliases"]

    def _normalize(kind: str) -> str:
        """Normalize kind: apply alias or return UNKNOWN_ISSUE_KIND_SENTINEL."""
        if kind in canonical_kinds:
            return kind
        if kind in aliases:
            return aliases[kind]
        return UNKNOWN_ISSUE_KIND_SENTINEL

    # 最優先: Machine-Readable Contract の issue_kind フィールド
    # #1878 P1 review: independent regex (unbound to the
    # `## Machine-Readable Contract` section, order-dependent on
    # contract_schema_version appearing before issue_kind) is replaced by the
    # shared, section-bound parser (mrc_contract_parser.parse_machine_readable_contract)
    # so this checker never diverges from validate_issue_body.py / #1135 P0 SSOT.
    mrc_result = parse_machine_readable_contract(body)
    if mrc_result.ok:
        # MRC section present and strictly valid: issue_kind (if any) is
        # authoritative. Do NOT fall back to labels/title — an MRC section
        # that omits issue_kind normalizes to UNKNOWN_ISSUE_KIND_SENTINEL via
        # _normalize("") below, same as an explicit unknown kind value.
        kind = str(mrc_result.get("issue_kind", "") or "").strip().rstrip('"\'')
        return _normalize(kind)
    if mrc_result.reason != _MRC_REASON_MISSING:
        # MRC section exists but is malformed (multiple sections, missing/
        # multiple YAML fences, YAML syntax error, duplicate key, non-mapping
        # root). This is NOT a legacy body — label/title fallback MUST NOT be
        # used (fail-closed per #1878 P1 review).
        return UNKNOWN_ISSUE_KIND_SENTINEL

    # fallback (legacy body without an MRC section at all): labels
    if "tracking" in labels or "parent" in labels:
        return _normalize("tracking")
    if "phase/research" in labels or title.startswith("調査:"):
        return "research"
    if "phase/implementation" in labels or title.startswith("実装:"):
        return "implementation"

    # fallback: title prefix
    if title.startswith(("実装:", "implement:", "perf:", "fix:", "docs:")):
        return "implementation"

    # Unknown: do NOT silently return "implementation"
    return UNKNOWN_ISSUE_KIND_SENTINEL


def check_c1_required_sections(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C1: 必須セクション存在チェック"""
    if issue_kind not in ("implementation",):
        return CheckResult.NA, []

    required = get_required_sections(issue_kind)
    failing = []
    for section in required:
        pattern = rf"^## {re.escape(section)}"
        if not re.search(pattern, body, re.MULTILINE):
            failing.append(f"必須セクション '## {section}' が存在しない")

    if failing:
        return CheckResult.FAIL, failing
    return CheckResult.PASS, []


def check_c2_stop_conditions(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C2: Stop Conditions 6 項目以上（implementation のみ）"""
    if issue_kind != "implementation":
        return CheckResult.NA, []

    section = extract_section(body, "Stop Conditions")
    if not section:
        return CheckResult.FAIL, ["## Stop Conditions セクションが存在しない"]

    bullet_count = sum(1 for _ in iter_top_level_stop_condition_items(section))
    if bullet_count < 6:
        return CheckResult.FAIL, [f"Stop Conditions の項目数が {bullet_count} 件（6 件以上必要）"]
    return CheckResult.PASS, []


def check_c3_ac_checkbox_format(body: str) -> tuple[str, list[str]]:
    """C3: AC が - [ ] 形式"""
    section = extract_section(body, "Acceptance Criteria")
    if not section:
        return CheckResult.FAIL, ["## Acceptance Criteria セクションが存在しないか空"]

    checkbox_lines = re.findall(r"^- \[[ xX]\]", section, re.MULTILINE)
    if not checkbox_lines:
        return CheckResult.FAIL, ["AC に `- [ ]` 形式のチェックボックス行が見つからない"]
    return CheckResult.PASS, []


def check_c4_vc_commands_present(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C4: VC コマンド存在チェック（#993: shared parser ベースに統一）

    canonical format: ```bash fenced block 内の $ コマンド行のみを認識。
    inline backtick VC や list-style VC (- `cmd`) は pass にしない。

    issue_kind == "parent"（canonical parent）の場合は existing_issue_readiness_v1
    profile と同じく `## Verification Commands` を必須セクションとしないため NA を返す
    （#1867: parent applicability defect の修正）。
    """
    if issue_kind == "parent":
        return CheckResult.NA, []

    section = extract_section(body, "Verification Commands")
    if not section:
        return CheckResult.FAIL, ["## Verification Commands セクションが存在しないか空"]

    if _VC_SECTION_PARSER_AVAILABLE:
        parse_result = _parse_vc_section(section)
        if parse_result.commands:
            return CheckResult.PASS, []
        # No canonical commands — check if bash fence exists with non-$ content
        if not parse_result.has_bash_fence:
            return CheckResult.FAIL, ["VC に ```bash fenced block が見当たらない（canonical format が必要）"]
        return CheckResult.FAIL, ["VC に実行可能コマンドが見当たらない（$ <command> 形式が必要）"]

    # Fallback (shared parser unavailable): $ lines in bash fences only
    bash_blocks = re.findall(r'```bash[^\n]*\n(.*?)```', section, re.DOTALL)
    for block in bash_blocks:
        for line in block.splitlines():
            if line.strip().startswith('$'):
                return CheckResult.PASS, []
    return CheckResult.FAIL, ["VC に実行可能コマンドが見当たらない（$ で始まる行が必要）"]


_VC_AC_COMMENT_RE = re.compile(
    r"^\s*#\s*(AC\d+(?:\s*,\s*AC\d+)*)\s*$",
    re.MULTILINE,
)


def _extract_vc_ac_refs(vc_section: str) -> set[str]:
    """
    Extract all AC numbers referenced in VC comment lines.

    Only pure comment lines are recognised — a line whose content (after
    stripping leading whitespace) is exactly ``# AC<N>`` or a comma-separated
    list ``# AC<N>, AC<M>, ...``.  Lines that merely contain ``#AC`` as part
    of prose, a URL fragment, a filename, or a shell command are intentionally
    excluded.

    Supports two forms:
    - Single:  # AC1
    - Grouped: # AC2, AC3, AC4  (comma-separated list on a single comment line)

    Range notation (e.g. # AC2-AC4) is NOT a recognised form; the whole line
    fails to match and is treated as unrecognised (no refs extracted).

    Returns a set of digit strings (e.g. {"1", "2", "3", "4"}).
    """
    refs: set[str] = set()
    for m in _VC_AC_COMMENT_RE.finditer(vc_section):
        refs.update(re.findall(r"AC(\d+)", m.group(1)))
    return refs


def check_c5_ac_vc_alignment(body: str, issue_kind: str = "implementation") -> tuple[str, list[str]]:
    """C5: AC と VC の番号一致チェック（#993: shared parser ベースに統一）

    VC 内の AC 参照として以下の形式を認識する:
    - Single:  # AC1
    - Grouped: # AC2, AC3, AC4  (カンマ区切りの grouped 表記 — #814)
    - Inline suffix: $ command  # AC1

    Range 表記 (# AC2-AC4) は本 Issue のスコープ外であり、サポートしない (AC3b)。

    `.github/ISSUE_TEMPLATE/research.yml` は AC セクションを番号なしの `- [ ]`
    チェックボックス形式で提示しており、AC[N] 番号を要求しない。
    issue_kind in ("research", "tracking") かつ AC 番号が一つも見つからない場合は
    NA を返す（C1/C4/C8/C9 と同じ issue_kind-aware パターン、#2480）。
    AC 番号が存在する research/tracking Issue では通常通り alignment を検証する。
    """
    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")

    if not ac_section or not vc_section:
        return CheckResult.NA, []

    # AC 番号を収集
    ac_numbers = set(re.findall(r'AC(\d+)', ac_section))

    # VC 内の AC 参照を収集
    # #993: shared parser を優先使用（inline suffix + grouped 両対応）。
    # fallback として _extract_vc_ac_refs() を使用（comment-only 形式）。
    if _VC_SECTION_PARSER_AVAILABLE:
        parse_result = _parse_vc_section(vc_section)
        # VcParseResult.ac_refs uses "AC1" format; extract digit-only for comparison
        # with ac_numbers (which are digit-only from re.findall(r'AC(\d+)', ac_section)).
        vc_ac_refs = {re.sub(r"^AC", "", ref) for ref in parse_result.ac_refs}
    else:
        vc_ac_refs = _extract_vc_ac_refs(vc_section)

    # AC 番号と VC 参照が全て一致するか
    if not ac_numbers:
        if issue_kind in ("research", "tracking"):
            return CheckResult.NA, []
        return CheckResult.FAIL, ["AC セクションに AC[N] 番号が見つからない"]

    missing_in_vc = ac_numbers - vc_ac_refs

    if missing_in_vc:
        missing_list = [f"AC{n}" for n in sorted(missing_in_vc)]
        return CheckResult.FAIL, [
            f"以下の AC が VC に '# AC<N>' 形式でコメント参照されていない: {', '.join(missing_list)}"
        ]

    return CheckResult.PASS, []


def check_c6_no_subjective_phrasing(body: str) -> tuple[str, list[str]]:
    """C6: 主観表現の混入チェック（AC / VC のみ）"""
    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")
    check_text = ac_section + "\n" + vc_section

    found = []
    for pattern in SUBJECTIVE_PATTERNS:
        if re.search(pattern, check_text):
            found.append(f"主観表現パターン '{pattern}' が AC/VC に含まれる")

    if found:
        return CheckResult.FAIL, found
    return CheckResult.PASS, []


def check_c7_required_skills_semantics(body: str) -> tuple[str, list[str]]:
    """C7: Required Skills にワークフロースキル / ドキュメントパスを含まない"""
    section = extract_section(body, "Required Skills")
    if not section or section.strip() in ("なし", "none", "N/A", ""):
        return CheckResult.PASS, []

    issues = []
    lines = section.splitlines()
    for line in lines:
        line = line.strip().lstrip("- ").strip()
        if not line:
            continue
        # ワークフロースキルチェック
        if line in WORKFLOW_SKILLS:
            issues.append(f"Required Skills にワークフロースキル '{line}' が含まれている（禁止）")
        # ドキュメントパスチェック
        if re.search(r"docs/|\.md$|^/", line):
            issues.append(f"Required Skills にドキュメントパス '{line}' が含まれている（禁止）")

    if issues:
        return CheckResult.FAIL, issues
    return CheckResult.PASS, []


def check_c8_outcome_concreteness(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C8: Outcome に抽象的パターンが含まれない

    issue_kind == "parent"（canonical parent）の場合は existing_issue_readiness_v1
    profile と同じく `## Outcome` を必須セクションとしないため NA を返す
    （#1867: parent applicability defect の修正）。
    """
    if issue_kind == "parent":
        return CheckResult.NA, []

    section = extract_section(body, "Outcome")
    if not section:
        return CheckResult.FAIL, ["## Outcome セクションが存在しないか空"]

    found = []
    for pattern in VAGUE_OUTCOME_PATTERNS:
        if re.search(pattern, section):
            found.append(f"Outcome に抽象的表現パターン '{pattern}' が含まれる")

    if found:
        return CheckResult.FAIL, found
    return CheckResult.PASS, []


def check_c9_runtime_applicability(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C9: Runtime Verification Applicability セクション存在チェック"""
    section = extract_section(body, "Runtime Verification Applicability")
    has_section = bool(re.search(r"^## Runtime Verification Applicability", body, re.MULTILINE))

    if issue_kind == "implementation":
        if not has_section:
            # セクション自体がない
            return CheckResult.LEGACY_MISSING, [(
                "## Runtime Verification Applicability セクションがない（レガシー Issue）"
            )]

        # decision: フィールドの確認
        decision_match = re.search(r'decision:\s*(\S+)', section)
        if not decision_match:
            return CheckResult.LEGACY_MISSING, ["decision: フィールドがない（レガシー Issue）"]

        decision = decision_match.group(1).strip()
        valid_decisions = {"not_applicable", "deferred", "immediate"}
        if decision not in valid_decisions:
            return (
                CheckResult.FAIL,
                [f"decision: '{decision}' が不正（not_applicable / deferred / immediate のいずれかであること）"]
            )

        return CheckResult.PASS, []

    elif issue_kind in ("research", "tracking"):
        if not has_section:
            return (
                CheckResult.WARN,
                [
                    "research/tracking Issue に ## Runtime Verification Applicability"
                    " セクションが存在しない（warn、approve を妨げない）"
                ]
            )
        return CheckResult.PASS, []

    else:
        if not has_section:
            return CheckResult.WARN, ["## Runtime Verification Applicability セクションが推奨（非実装 Issue）"]
        return CheckResult.PASS, []


def check_c10_deferred_destination(body: str) -> tuple[str, list[str]]:
    """C10: deferred の検証先不明チェック"""
    section = extract_section(body, "Runtime Verification Applicability")
    if not section:
        return CheckResult.NA, []

    if "decision: deferred" not in section:
        return CheckResult.PASS, []

    # deferred の場合は deferred_destination または deferred_verification_condition が必要
    has_destination_type = bool(re.search(r"destination_type:", section))
    has_destination_ref = bool(re.search(r"destination_ref:", section))
    has_verification_condition = bool(re.search(r"deferred_verification_condition:", section))

    if not (has_destination_type and has_destination_ref) and not has_verification_condition:
        return CheckResult.FAIL, [
            "decision: deferred なのに deferred_destination（destination_type + destination_ref）または "
            "deferred_verification_condition が欠けている"
        ]
    return CheckResult.PASS, []


def check_c11_decision_tag_consistency(body: str) -> tuple[str, list[str]]:
    """C11: decision と runtime-verification タグの整合チェック"""
    rva_section = extract_section(body, "Runtime Verification Applicability")
    if not rva_section:
        return CheckResult.NA, []

    ac_section = extract_section(body, "Acceptance Criteria")

    # decision を取得
    decision_match = re.search(r"decision:\s*(\S+)", rva_section)
    if not decision_match:
        return CheckResult.NA, []

    decision = decision_match.group(1).strip()
    has_rv_tag = bool(re.search(r"<!--\s*runtime-verification:\s*true\s*-->", ac_section))

    if decision == "immediate" and not has_rv_tag:
        return CheckResult.FAIL, [
            "decision: immediate なのに AC に <!-- runtime-verification: true --> タグが 1 つもない（blocker）"
        ]
    elif decision in ("not_applicable", "deferred") and has_rv_tag:
        return CheckResult.FAIL, [
            f"decision: {decision} なのに AC に <!-- runtime-verification: true --> タグが存在する（矛盾 blocker）"
        ]

    return CheckResult.PASS, []


def _ac_vc_commands_from_parse_result(evaluator, parse_result):
    """Issue #2961: ``ac_vc_commands`` for the shared exemption helper, built
    by the shared evaluator's pure ``build_ac_vc_commands`` from the existing
    canonical parser result (``VcParseResult.commands``). ``None`` (never
    ``{}``) when no canonical parse result exists, which makes the evaluator
    fail closed (no exemption)."""
    if parse_result is None:
        return None
    # An evaluator without the shared helper (e.g. a minimal test stub) can
    # never exempt: fail closed instead of raising.
    build = getattr(evaluator, "build_ac_vc_commands", None)
    if build is None:
        return None
    return build(parse_result.commands)


def _ac_vc_commands_for_body(evaluator, vc_section: str):
    """Parse the VC section with the same canonical parser as C4/C5/C15 and
    derive ``ac_vc_commands``. The review-issue ``_VC_SECTION_PARSER_AVAILABLE
    == False`` regex fallback cannot recover command bodies, so ``None`` is
    returned there and no exemption is ever applied (Issue #2961)."""
    if not _VC_SECTION_PARSER_AVAILABLE:
        return None
    return _ac_vc_commands_from_parse_result(evaluator, _parse_vc_section(vc_section or ""))


def _evaluate_issue_risk_trigger_for_body(
    body: str, issue_kind: str
) -> tuple[Optional[dict], Optional[Exception]]:
    """Run the shared ``evaluate_issue_risk_trigger`` for C14 and the Issue #2961
    exemption carrier. ``(None, None)``: not applicable; ``(None, exc)``:
    ``PolicyLoadError``; otherwise ``(verdict, None)``. Only transports the
    VC command bodies / AC section; adds no judgement logic."""
    if issue_kind != "implementation":
        return None, None

    allowed_path_entries = pc_extract_allowed_paths(body)
    if not allowed_path_entries:
        return None, None

    rva_section = extract_section(body, "Runtime Verification Applicability")
    decision_match = re.search(r"decision:\s*(\S+)", rva_section) if rva_section else None
    declared_decision = decision_match.group(1).strip() if decision_match else None

    evaluator = _load_extension_surface_policy_matcher()
    if evaluator is None:
        return None, None

    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")
    ac_vc_commands = _ac_vc_commands_for_body(evaluator, vc_section)

    try:
        verdict = evaluator.evaluate_issue_risk_trigger(
            allowed_path_entries=allowed_path_entries,
            declared_decision=declared_decision,
            rva_section_text=rva_section or "",
            ac_section_text=ac_section or "",
            ac_vc_commands=ac_vc_commands,
        )
    except evaluator.PolicyLoadError as exc:
        return None, exc
    return verdict, None


def get_extension_surface_issue_time_exemption_carrier(body: str, issue_kind: str) -> list[str]:
    """Issue #2961: non-blocking carrier lines for an APPLIED comment-only
    exemption (``[]`` when none). Transcribes the shared evaluator's
    ``issue_time_exemptions`` verbatim via its shared formatter."""
    verdict, policy_error = _evaluate_issue_risk_trigger_for_body(body, issue_kind)
    if policy_error is not None or verdict is None:
        return []
    exemptions = verdict.get("issue_time_exemptions") or []
    if not exemptions:
        return []
    evaluator = _load_extension_surface_policy_matcher()
    if evaluator is None:
        return []
    return evaluator.format_issue_time_exemption_lines(exemptions)


def check_c14_extension_surface_risk_trigger(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C14 (Issue #2290): declared Allowed Paths vs. extension-surface risk-trigger policy.

    Uses the shared evaluator (`scripts/agent-guards/extension_surface_policy_matcher.py`)
    to detect a syntactic candidate overlap between the Issue's declared
    Allowed Paths and `docs/dev/extension-surface-runtime-policy.yaml`
    selectors, and (when `decision: immediate` is declared) whether the
    Runtime Verification Applicability contract's required immediate
    fields are present. This is candidate discovery only -- not a semantic
    diff judgment of the actual PR change (Issue #2290 Out of Scope).
    """
    if issue_kind != "implementation":
        return CheckResult.NA, []

    verdict, policy_error = _evaluate_issue_risk_trigger_for_body(body, issue_kind)
    if policy_error is not None:
        exc = policy_error
        # Distinguishable from the "matcher module not found" NA above
        # (Issue #2290 P1-2, PR #2335): WARN (not NA, not FAIL) surfaces
        # that the policy itself was unusable -- a non-blocking advisory
        # finding rather than silently looking identical to "no candidate
        # match found" (NA) or forcing a hard block (FAIL).
        return CheckResult.WARN, [f"extension-surface risk-trigger policy unavailable: {exc}"]
    if verdict is None:
        return CheckResult.NA, []

    if verdict["verdict"] == "needs_fix":
        return CheckResult.FAIL, verdict["reasons"]
    return CheckResult.PASS, []


def check_c15_runtime_assertion_binding_coverage(body: str, issue_kind: str) -> tuple[str, list[str]]:
    """C15 (Issue #2771): hard-required verification profile assertion
    binding completeness.

    Uses the shared evaluator's
    `evaluate_runtime_assertion_binding_coverage()` to verify that every
    `(verification_profile_id, assertion_id)` pair derived purely from
    `matched_rules[].enforcement == "hard"` has an explicit
    `runtime_assertion_bindings` declaration in the Issue's Runtime
    Verification Applicability section, and that each declared binding's
    `ac` is referentially valid (exists, is declared in `applicable_acs`,
    is consistent with the decision-level runtime-verification tag, and has
    a canonical `# AC<N>` reference in `## Verification Commands`).

    This is a STRUCTURAL COMPLETENESS check only -- it never judges whether
    the bound VC actually proves the assertion's semantic postcondition
    (Issue #2771 Outcome / AC10). Mirrors `check_c14_extension_surface_
    risk_trigger`'s NA/WARN/FAIL shape so the two checks share the same
    call-site pattern.
    """
    verdict, policy_error = _evaluate_runtime_assertion_binding_coverage_for_body(body, issue_kind)
    if policy_error is not None:
        # Issue #2771 AC6: a policy integrity defect (dangling profile
        # reference / duplicate assertion id within a profile) is not
        # Issue-author-fixable -- WARN (not FAIL, not NA), mirroring C14's
        # existing PolicyLoadError handling above.
        return CheckResult.WARN, [f"runtime assertion binding coverage policy unavailable: {policy_error}"]
    if verdict is None:
        return CheckResult.NA, []

    if verdict["verdict"] == "needs_fix":
        return CheckResult.FAIL, verdict["reasons"]
    return CheckResult.PASS, []


def _evaluate_runtime_assertion_binding_coverage_for_body(
    body: str, issue_kind: str
) -> tuple[Optional[dict], Optional[Exception]]:
    """Run the shared evaluator for C15 and the Issue #2852 carrier.

    Returns ``(verdict, policy_error)``: ``(None, None)`` when C15 is not
    applicable (non-implementation Issue, no Allowed Paths, evaluator not
    loadable); ``(None, exc)`` on a policy integrity failure
    (``PolicyLoadError``); otherwise ``(verdict, None)``.
    """
    if issue_kind != "implementation":
        return None, None

    allowed_path_entries = pc_extract_allowed_paths(body)
    if not allowed_path_entries:
        return None, None

    rva_section = extract_section(body, "Runtime Verification Applicability")
    ac_section = extract_section(body, "Acceptance Criteria")
    vc_section = extract_section(body, "Verification Commands")

    parse_result = None
    if _VC_SECTION_PARSER_AVAILABLE and vc_section:
        parse_result = _parse_vc_section(vc_section)
        ac_vc_refs = {re.sub(r"^AC", "", ref) for ref in parse_result.ac_refs}
    else:
        ac_vc_refs = _extract_vc_ac_refs(vc_section or "")

    evaluator = _load_extension_surface_policy_matcher()
    if evaluator is None:
        return None, None

    # Issue #2961: reuse the existing parse result (no second parse). Without
    # the canonical parser (regex fallback) `ac_vc_commands` is None and the
    # shared evaluator never applies the comment-only exemption.
    if _VC_SECTION_PARSER_AVAILABLE:
        ac_vc_commands = _ac_vc_commands_from_parse_result(
            evaluator,
            parse_result if parse_result is not None else _parse_vc_section(vc_section or ""),
        )
    else:
        ac_vc_commands = None

    try:
        verdict = evaluator.evaluate_runtime_assertion_binding_coverage(
            allowed_path_entries=allowed_path_entries,
            rva_section_text=rva_section or "",
            ac_section_text=ac_section or "",
            ac_vc_refs=ac_vc_refs,
            ac_vc_commands=ac_vc_commands,
        )
    except evaluator.PolicyLoadError as exc:
        return None, exc
    return verdict, None


def get_runtime_assertion_disposition_carrier(body: str, issue_kind: str) -> list[str]:
    """Issue #2852 AC9: non-blocking disposition classification carrier lines
    (one line per binding) for ``non_blocking_improvements[].evidence``.

    Companion to ``check_c15_runtime_assertion_binding_coverage`` (the
    unchanged blocking gate): returns ``[]`` unless the structural evaluation
    APPROVES, so the carrier never decorates a FAIL / policy-unavailable WARN
    primary result, and only when at least one binding is explicit /
    ``not_applicable`` / compat (a pure legacy 3-field input emits nothing).
    The text is produced by the shared evaluator module's formatter, the same
    one ``issue-contract-review`` uses. Classification only -- never runtime
    PASS evidence.
    """
    verdict, policy_error = _evaluate_runtime_assertion_binding_coverage_for_body(body, issue_kind)
    if policy_error is not None or verdict is None:
        return []
    evaluator = _load_extension_surface_policy_matcher()
    if evaluator is None:
        return []
    return evaluator.format_runtime_assertion_disposition_carrier_lines(verdict)


def get_extension_surface_candidate_advisories(body: str, issue_kind: str) -> list[str]:
    """Issue #2339: non-blocking `unclassified_candidate` advisory messages.

    Companion to ``check_c14_extension_surface_risk_trigger`` (which remains
    the unchanged blocking C14 gate) -- this is wired separately as a
    ``non_blocking_improvements`` entry (via ``_add_warning``) so it can
    never affect ``CheckResult``/verdict, and so ``review-issue`` and
    ``issue-contract-review`` expose the same classification + advisory
    info for the same fixture body (Issue #2339 AC7 parity requirement,
    same shared-evaluator structural-parity pattern as C14 itself).
    """
    if issue_kind != "implementation":
        return []

    allowed_path_entries = pc_extract_allowed_paths(body)
    if not allowed_path_entries:
        return []

    evaluator = _load_extension_surface_policy_matcher()
    if evaluator is None:
        return []

    try:
        policy_evaluation = evaluator.evaluate_allowed_paths(allowed_path_entries)
    except evaluator.PolicyLoadError:
        # A policy-load failure is already surfaced as a blocking C14 WARN
        # finding by check_c14_extension_surface_risk_trigger(); do not
        # double-report it here as a non-blocking advisory.
        return []

    return list(policy_evaluation.get("advisories") or [])


def check_c13_vc_preflight_decision_consistency(
    vc_preflight_json_path: Optional[str] = None,
) -> tuple[str, list[str]]:
    """C13: VC preflight JSON (if provided) has consistent decision values.

    Applicability: only if --vc-preflight-json path is provided.
    If not provided, return NA (not PASS).

    Checks:
      - All entries in vc_preflight JSON results have valid decision field
      - decision values are in (go, blocked, human_judgment)
      - skipped results have verification_owner and runtime_verification_required fields

    戻り値: (CheckResult, list[failure_message])

    Note: category is regression_gate for both pass and fail outcomes.
    The pass/fail distinction is carried by classification (expected_pass vs blocked)
    and decision (go vs blocked). Downstream consumers MUST read classification
    for the routing-canonical pass/fail signal.
    """
    if not vc_preflight_json_path:
        return CheckResult.NA, []

    try:
        with open(vc_preflight_json_path) as f:
            preflight_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return CheckResult.FAIL, [f"Failed to load or parse VC preflight JSON: {vc_preflight_json_path}"]

    issues = []
    results = preflight_data.get("results", [])

    valid_decisions = {"go", "blocked", "human_judgment"}

    for result in results:
        decision = result.get("decision")
        if decision not in valid_decisions:
            issues.append(f"AC {result.get('ac', 'UNKNOWN')}: invalid decision '{decision}'")

        # Check skipped result metadata
        if result.get("classification") == "skipped":
            if "verification_owner" not in result:
                issues.append(f"AC {result.get('ac', 'UNKNOWN')}: skipped result missing verification_owner")
            if "runtime_verification_required" not in result:
                issues.append(f"AC {result.get('ac', 'UNKNOWN')}: skipped result missing runtime_verification_required")

    if issues:
        return CheckResult.FAIL, issues
    return CheckResult.PASS, []


def check_c12_product_trace_fields_structure(body: str) -> tuple[str, list[str]]:
    """C12: Product Spec / task-lineage Issue で trace fields の構造を検査する。

    Applicable 条件: body に `## Product Spec Context`、`## Machine-Readable Contract` 内の
    product trace fields、または `product_spec_id` / `requirement_id` / `source_task_id`
    の言及があるとき。

    Applicable な場合、以下を要求:
      - product_spec_id / requirement_id / source_task_id の 3 fields が全て存在し non-placeholder
      - requirement_id は REQ-\\d{3,} 形式
      - source_task_id は (T|TASK-)\\d{3,} 形式
      - product_spec_id は non-empty / non-placeholder のみ確認（canonical format は固定しない）
    """
    # Applicability detection (PR #390 REQUEST_CHANGES blocker 1 対応):
    # 本文全体に trace field 語を含むだけで applicable にすると、Out of Scope や
    # spec 説明文に出現する言及まで誤って C12 対象にしてしまう。Machine-Readable
    # Contract の YAML / Product Spec Context セクションに `<field>:` 形式で
    # **構造化された** trace field が存在する場合に限って applicable とする。
    #
    # PR #390 review-2 blocker 3 対応: MRC YAML は yaml.safe_load で parse し、
    # inline comment / quote / null / folded scalar を YAML semantics で正しく扱う。
    # parse 失敗時は regex fallback に降りる（既存挙動を破壊しない）。
    # #1135 P0: MRC は本文全体ではなく `## Machine-Readable Contract` セクションに
    # 束縛して strict に parse する（共通 parser）。decoy YAML / 重複 key / fence 複数 /
    # mapping でない root は fail-closed（mrc_result.ok == False）となり、docs 免除や
    # trace field 取得に利用されない。LP002 と同一 parser を共有する。
    mrc_result = parse_machine_readable_contract(body)
    mrc_data: dict = mrc_result.data if (mrc_result.ok and isinstance(mrc_result.data, dict)) else {}

    mrc_change_kind: Optional[str] = None
    if mrc_result.ok:
        raw_change_kind = mrc_data.get("change_kind")
        if raw_change_kind is not None:
            mrc_change_kind = str(raw_change_kind).strip().lower()

    psc_section = extract_section(body, "Product Spec Context")
    has_product_spec_context = bool(psc_section)

    _trace_keys = ("product_spec_id", "requirement_id", "source_task_id")
    mrc_has_trace = mrc_result.ok and any(k in mrc_data for k in _trace_keys)
    psc_has_trace = bool(re.search(
        r"\b(product_spec_id|requirement_id|source_task_id)\s*:",
        psc_section,
    ))
    has_trace_field_mention = mrc_has_trace or psc_has_trace

    # task-lineage marker: structured field か、tasks.md / generated task 由来の宣言文
    has_task_lineage_marker = bool(re.search(
        r"^\s*-?\s*(generated_from_task|task_lineage|source_task)\s*:",
        body,
        re.MULTILINE,
    )) or bool(re.search(
        r"\b(generated\s+from\s+tasks?\.md|from\s+tasks?\.md|task_materialization|generated\s+task)\b",
        body,
        re.IGNORECASE,
    ))

    # #1135 P1a: docs-only 免除は自己申告 change_kind: docs だけでは不十分。
    # Allowed Paths が全て documentation 分類のときに限り docs_only とする。
    allowed_paths = pc_extract_allowed_paths(body)
    docs_only = (mrc_change_kind == "docs") and is_docs_only_allowed_paths(allowed_paths)

    # #1135 P1a: change_kind: docs を自己申告しながら Allowed Paths に code/runtime
    # path を含む矛盾 issue は docs 免除を適用せず、product-spec signal があるとき
    # blocking fail にする（silent な n/a にしない）。
    if (
        mrc_change_kind == "docs"
        and has_code_or_runtime_scope(allowed_paths)
        and (has_product_spec_context or has_trace_field_mention or has_task_lineage_marker)
    ):
        return CheckResult.FAIL, [
            "change_kind: docs だが Allowed Paths に code/runtime path が含まれる"
            "（docs-only 免除は不適用）。product trace fields を宣言するか change_kind を見直す",
        ]

    psc_alone_applies = has_product_spec_context and not docs_only
    applicable = psc_alone_applies or has_trace_field_mention or has_task_lineage_marker
    if not applicable:
        return CheckResult.NA, []

    # Extract trace fields:
    # 1) MRC YAML が parse 成功した場合: mrc_parsed[field] を優先（inline comment 等を YAML semantics で除去）
    # 2) それ以外（PSC セクション / parse 失敗時）: regex fallback で structured_trace_text を走査
    # (PR #390 REQUEST_CHANGES blocker 1 + review-2 blocker 3 対応)
    def _extract_field(field_name: str) -> Optional[str]:
        if mrc_result.ok and field_name in mrc_data:
            v = mrc_data.get(field_name)
            if v is None:
                return None
            return str(v).strip()
        m = re.search(
            rf'^\s*-?\s*{re.escape(field_name)}\s*:\s*["\']?([^"\'\n#]*?)["\']?\s*(?:#.*)?$',
            psc_section,
            re.MULTILINE,
        )
        if m:
            return m.group(1).strip()
        return None

    def _is_placeholder(v: Optional[str]) -> bool:
        if v is None:
            return True
        norm = v.strip().lower()
        if norm in PLACEHOLDER_VALUES:
            return True
        if PLACEHOLDER_PATTERN.match(v):
            return True
        return False

    product_spec_id = _extract_field("product_spec_id")
    requirement_id = _extract_field("requirement_id")
    source_task_id = _extract_field("source_task_id")

    failures = []
    if _is_placeholder(product_spec_id):
        failures.append("product_spec_id が欠落または placeholder")
    if _is_placeholder(requirement_id):
        failures.append("requirement_id が欠落または placeholder")
    elif not REQUIREMENT_ID_PATTERN.match(requirement_id):
        failures.append(f"requirement_id '{requirement_id}' が REQ-\\d{{3,}} 形式に一致しない")
    if _is_placeholder(source_task_id):
        failures.append("source_task_id が欠落または placeholder")
    elif not SOURCE_TASK_ID_PATTERN.match(source_task_id):
        failures.append(f"source_task_id '{source_task_id}' が (T|TASK-)\\d{{3,}} 形式に一致しない")

    if failures:
        return CheckResult.FAIL, failures
    return CheckResult.PASS, []


def _bullet_tokens(section: str) -> set[str]:
    """Extract tokens from bullet lines in a section using 3-pass tokenization.

    Pass 1: backtick-quoted tokens (e.g. `foo.py`)
    Pass 2: bare path tokens matching PATH_TOKEN_RE (with extension or prefix)
    Pass 3: ASCII significant tokens matching SIGNIFICANT_TOKEN_RE (lowercased, STOP_TOKENS excluded)

    Bullet markers supported: "- ", "* ", "+ " and indented variants (up to 3 spaces).
    Scope: ASCII / English natural-language tokens only.
    Japanese text without path/backtick tokens yields 0 tokens (known limitation).
    Tokens are lowercased for normalization.
    """
    tokens: set[str] = set()
    for line in section.splitlines():
        m = BULLET_RE.match(line)
        if not m:
            continue

        content = m.group(1)  # content after bullet marker

        # Pass 1: backtick-quoted tokens
        for tok in re.findall(r"`([^`]+)`", content):
            cleaned = tok.strip().rstrip(PATH_TOKEN_STRIP_CHARS)
            if cleaned:
                # normalize ./prefix
                if cleaned.startswith("./"):
                    cleaned = cleaned[2:]
                tokens.add(cleaned.lower())

        # Pass 2: bare path tokens
        for match in PATH_TOKEN_RE.finditer(content):
            tok = match.group(0).rstrip(PATH_TOKEN_STRIP_CHARS)
            if tok:
                if tok.startswith("./"):
                    tok = tok[2:]
                tokens.add(tok.lower())

        # Pass 3: ASCII significant tokens
        for match in SIGNIFICANT_TOKEN_RE.finditer(content):
            tok = match.group(0).lower()
            if tok not in STOP_TOKENS:
                tokens.add(tok)

    return tokens


def detect_warning_scope_cvs_in_scope_mismatch(body: str, result: CheckerResult) -> None:
    """non_blocking: Current Validated Scope と In Scope の bullet 集合の乖離検出。"""
    cvs = extract_section(body, "Current Validated Scope")
    in_scope = extract_section(body, "In Scope")
    if not cvs or not in_scope:
        return

    cvs_tokens = _bullet_tokens(cvs)
    in_scope_tokens = _bullet_tokens(in_scope)
    if not cvs_tokens or not in_scope_tokens:
        return

    overlap = cvs_tokens & in_scope_tokens
    union = cvs_tokens | in_scope_tokens
    jaccard = len(overlap) / len(union) if union else 1.0
    # Substantial divergence: less than JACCARD_THRESHOLD overlap
    if union and jaccard < JACCARD_THRESHOLD:
        missing_from_cvs = sorted(in_scope_tokens - cvs_tokens)[:10]
        missing_from_in_scope = sorted(cvs_tokens - in_scope_tokens)[:10]
        # evidence: list[str] — machine-readable shape (Blocker 1: keep as list[str])
        evidence = [
            f"scope token jaccard {jaccard:.3f} < {JACCARD_THRESHOLD}",
            f"Current Validated Scope tokens: {sorted(cvs_tokens)[:10]}",
            f"In Scope tokens: {sorted(in_scope_tokens)[:10]}",
        ]
        # details: dict — structured info separated from evidence (Blocker 1: new field)
        details = {
            "jaccard": round(jaccard, 4),
            "overlap": sorted(overlap)[:10],
            "missing_from_cvs": missing_from_cvs,
            "missing_from_in_scope": missing_from_in_scope,
        }
        result.non_blocking_improvements.append({
            "code": "scope_cvs_in_scope_mismatch",
            "severity": "warning",
            "evidence": evidence,
            "details": details,
            "suggested_action": (
                "Current Validated Scope と In Scope の対象ファイル/対象範囲を揃えるか、"
                "乖離理由を Background / Scope Delta に記載する"
            ),
        })


def detect_warning_vc_untracked_false_negative(body: str, result: CheckerResult) -> None:
    """non_blocking: `git status --porcelain | grep -v "^?"` 型の偽陰性パターン検出。"""
    vc = extract_section(body, "Verification Commands")
    if not vc:
        return

    matches = re.findall(
        r"git\s+status\s+--porcelain[^\n]*\|\s*grep\s+-v\s+[\"']?\^[?][\"']?",
        vc,
    )
    if matches:
        _add_warning(
            result,
            code="vc_untracked_false_negative_pattern",
            severity="warning",
            evidence=[m[:120] for m in matches[:3]],
            suggested_action=(
                "`grep -v \"^?\"` は untracked 行を除外するため、unstaged 変更を見逃す。検証対象を literal"
                " で列挙する形式に変更する"
            ),
        )


def detect_warning_vc_negative_grep_without_literal_inventory(body: str, result: CheckerResult) -> None:
    """non_blocking: 削除確認 VC が削除対象 literal を列挙せず否定 grep のみで完了確認している形を検出。"""
    vc = extract_section(body, "Verification Commands")
    ac = extract_section(body, "Acceptance Criteria")
    if not vc:
        return

    # Triggers: deletion intent in AC/VC + negation grep pattern in VC
    deletion_intent = bool(re.search(r"(削除|deletion|removed?\b|delete\b)", ac + "\n" + vc, re.IGNORECASE))
    negation_patterns = re.findall(
        r"(?:^|\n)\s*\$?\s*!\s*(?:rg|grep)\b[^\n]+",
        vc,
    )
    if not deletion_intent or not negation_patterns:
        return

    # Heuristic: literal inventory absent if there's no `test -f <path>` or rg listing deletion targets.
    # Look for explicit removal-target enumeration: `removed:` section, or `- [ ] AC.*削除.*<literal>`
    has_literal_inventory = bool(re.search(
        r"(removed_paths|削除対象|deleted_files|literal_list)\s*:",
        vc + "\n" + body,
    ))
    if not has_literal_inventory:
        _add_warning(
            result,
            code="vc_negative_grep_without_literal_inventory",
            severity="warning",
            evidence=[m.strip()[:120] for m in negation_patterns[:3]],
            suggested_action=(
                "削除確認 VC では削除対象 literal を明示的に列挙し、`test ! -f <path>` 形式または `removed_paths:`"
                " リストで個別確認する"
            ),
        )


def get_required_section_placeholders(
    issue_kind: str,
    template_path: str = ".github/ISSUE_TEMPLATE/implementation.yml",
) -> dict[str, str]:
    """ISSUE_TEMPLATE の attributes.label → attributes.placeholder map を返す。

    PR #390 REQUEST_CHANGES blocker 3 対応:
    C1 skeleton を `## <label>` + 固定 TODO ではなく、template の placeholder 由来にする。
    """
    if issue_kind != "implementation":
        return {}

    import os
    import yaml as _yaml
    if not os.path.exists(template_path):
        return {}
    try:
        with open(template_path) as f:
            tmpl = _yaml.safe_load(f)
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    for item in tmpl.get("body", []):
        if item.get("type") == "markdown":
            continue
        attrs = item.get("attributes", {})
        label = attrs.get("label")
        if not label:
            continue
        placeholder = attrs.get("placeholder") or attrs.get("description") or ""
        mapping[label] = placeholder
    return mapping


def generate_c1_missing_section_skeleton(
    missing_sections: list[str],
    placeholders: Optional[dict[str, str]] = None,
) -> list[dict]:
    """C1 fail 時に missing section ごとの diff_proposal.add エントリを生成する。

    各エントリは sentinel marker `missing_section_skeleton` を含み、impl-review-loop / issue-author
    が機械的に skeleton を挿入できる形式にする。skeleton 本文は ISSUE_TEMPLATE の
    `attributes.placeholder` を優先し、無い場合のみ fallback TODO 文字列を使う
    (PR #390 REQUEST_CHANGES blocker 3 対応)。
    """
    placeholders = placeholders or {}
    entries = []
    for section in missing_sections:
        placeholder_text = placeholders.get(section, "").strip()
        if placeholder_text:
            body_block = placeholder_text
        else:
            body_block = (
                f"<!-- TODO: {section} を記述する。"
                "詳細は .github/ISSUE_TEMPLATE/implementation.yml 参照 -->"
            )
        entries.append({
            "kind": "missing_section_skeleton",
            "section": section,
            "placeholder_source": "template" if placeholder_text else "fallback_todo",
            "skeleton": f"## {section}\n\n{body_block}\n",
        })
    return entries


def _make_self_checker_evidence(
    result: "CheckerResult",
    *,
    rule_id: str,
    category: str,
) -> list[dict]:
    return [
        {
            "source_check": "check_issue_contract",
            "rule_id": rule_id,
            "category": category,
            "artifact_path": "check_issue_contract.py",
            "artifact_schema": "CHECK_ISSUE_CONTRACT_V1",
            "body_sha256": result.body_sha256,
            "iteration_id": "check_issue_contract_current",
            "line_start": None,
            "line_end": None,
        }
    ]


def _make_product_spec_checker_evidence(
    result: "CheckerResult",
    *,
    rule_id: str,
    body_sha256: str,
) -> list[dict]:
    return [
        {
            "source_check": "check_product_spec_contract",
            "rule_id": rule_id,
            "category": "product_spec_contract",
            "artifact_path": "check_product_spec_contract.py",
            "artifact_schema": "product_spec_check/v1",
            "body_sha256": body_sha256,
            "iteration_id": "check_product_spec_contract_current",
            "line_start": None,
            "line_end": None,
        }
    ]


def _run_product_spec_check(
    body: str,
    *,
    body_file_path: str | None,
) -> tuple[dict | None, int, str | None]:
    script_path = (
        Path(__file__).resolve().parent.parent.parent
        / "issue-contract-review"
        / "scripts"
        / "check_product_spec_contract.py"
    )

    temp_path: str | None = None
    if body_file_path is not None:
        try:
            existing_body = Path(body_file_path).read_text(encoding="utf-8")
        except OSError:
            existing_body = None
        if existing_body != body:
            body_file_path = None

    if body_file_path is None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".md") as tmp:
            tmp.write(body)
            temp_path = tmp.name
        body_file_path = temp_path

    cmd = [
        sys.executable,
        str(script_path),
        "--issue-number",
        "0",
        "--repo",
        "local/review-issue",
        "--body-file",
        body_file_path,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, -1, "timeout"
    finally:
        if temp_path:
            with contextlib.suppress(OSError):
                Path(temp_path).unlink()

    try:
        return json.loads(completed.stdout), completed.returncode, None
    except json.JSONDecodeError:
        return None, completed.returncode, "malformed_json"


def _apply_product_spec_check(
    result: "CheckerResult",
    *,
    body: str,
    body_file_path: str | None,
) -> None:
    if evaluate_product_spec_payload is None:
        issue = "product spec checker evaluator import failed"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    payload, rc, error = _run_product_spec_check(body, body_file_path=body_file_path)
    if error:
        issue = f"product spec checker failed closed: {error}"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    if rc not in (0, 1):
        issue = f"product spec checker failed closed: nonzero_exit:{rc}"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    if not isinstance(payload, dict):
        issue = "product spec checker failed closed: payload_not_object"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    payload_body_sha = payload.get("body_sha256")
    if payload_body_sha != result.body_sha256:
        issue = "product spec checker body_sha256 mismatch"
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    gate = evaluate_product_spec_payload(
        payload,
        issue_url="https://github.com/local/review-issue/issues/0",
        body_sha256=result.body_sha256,
        exit_code=rc,
    )
    if gate.get("routing_action") == "refresh_contract_snapshot":
        issue = gate.get("reason", "product spec checker invariant violation")
        _append_findings(
            result,
            [issue],
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id="PS001",
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.append(issue)
        return

    if (
        gate.get("applicability") == "applicable"
        and gate.get("decision") in {"fail", "human_judgment"}
    ):
        blocked_reasons = payload.get("blocked_reasons", [])
        issues = [
            reason.get("excerpt", "product spec checker blocked")
            for reason in blocked_reasons
            if isinstance(reason, dict)
        ] or ["product spec checker blocked"]
        rule_id = (
            blocked_reasons[0].get("rule_id")
            if blocked_reasons and isinstance(blocked_reasons[0], dict)
            else "PS001"
        )
        _append_findings(
            result,
            issues,
            deterministic_domain_key="product_spec_contract",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=_make_product_spec_checker_evidence(
                result,
                rule_id=rule_id,
                body_sha256=result.body_sha256,
            ),
            reviewer_blocker_code="PRODUCT_SPEC",
        )
        result.blocking_issues.extend(issues)


def run_checks(
    body: str,
    labels: str = "",
    title: str = "",
    vc_preflight_json_path: Optional[str] = None,
    body_file_path: Optional[str] = None,
) -> CheckerResult:
    """Run all C1-C13 checks and return structured result."""
    issue_kind = detect_issue_kind(body, labels, title)
    result = CheckerResult(issue_kind=issue_kind)
    result.body_sha256 = _sha256_prefixed(body)

    checks = result.deterministic_checks

    # C1
    checks.C1_required_sections, issues = check_c1_required_sections(body, issue_kind)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="required_sections",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C1_required_sections in (CheckResult.FAIL, CheckResult.LEGACY_MISSING),
    )
    # C1 fail 時: missing section ごとの skeleton を diff_proposal.add に同梱する
    if checks.C1_required_sections == CheckResult.FAIL and issues:
        missing_sections = []
        for msg in issues:
            m = re.search(r"必須セクション '## ([^']+)' が存在しない", msg)
            if m:
                missing_sections.append(m.group(1))
        if missing_sections:
            placeholders = get_required_section_placeholders(issue_kind)
            result.diff_proposal["add"].extend(
                generate_c1_missing_section_skeleton(missing_sections, placeholders)
            )

    # C2
    checks.C2_stop_conditions_6, issues = check_c2_stop_conditions(body, issue_kind)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="stop_conditions",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C2_stop_conditions_6 == CheckResult.FAIL,
    )

    # C3
    checks.C3_ac_checkbox_format, issues = check_c3_ac_checkbox_format(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="ac_checkbox_format",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C3_ac_checkbox_format == CheckResult.FAIL,
    )

    # C4
    checks.C4_vc_commands_present, issues = check_c4_vc_commands_present(body, issue_kind)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="vc_command_format",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
        blocking=checks.C4_vc_commands_present == CheckResult.FAIL,
        checker_evidence=(
            _make_self_checker_evidence(
                result,
                rule_id="C4_vc_commands_present",
                category="vc_command_format",
            )
            if checks.C4_vc_commands_present == CheckResult.FAIL and issues
            else []
        ),
        reviewer_blocker_code="C4",
    )

    # annotation-aware VC parse (Issue #599)
    vc_section = extract_section(body, "Verification Commands")
    if vc_section:
        result.parsed_vc_commands = parse_vc_commands(vc_section)

    # C5
    checks.C5_ac_vc_number_alignment, issues = check_c5_ac_vc_alignment(body, issue_kind)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="vc_number_alignment",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
        blocking=checks.C5_ac_vc_number_alignment == CheckResult.FAIL,
        checker_evidence=(
            _make_self_checker_evidence(
                result,
                rule_id="C5_ac_vc_number_alignment",
                category="vc_number_alignment",
            )
            if checks.C5_ac_vc_number_alignment == CheckResult.FAIL and issues
            else []
        ),
        reviewer_blocker_code="C5",
    )

    # C6
    checks.C6_no_subjective_phrasing, issues = check_c6_no_subjective_phrasing(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="subjective_phrasing",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C6_no_subjective_phrasing == CheckResult.FAIL,
    )

    # C7
    checks.C7_required_skills_semantics, issues = check_c7_required_skills_semantics(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="required_skills_semantics",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C7_required_skills_semantics == CheckResult.FAIL,
    )

    # C8
    checks.C8_outcome_concreteness, issues = check_c8_outcome_concreteness(body, issue_kind)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="outcome_concreteness",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C8_outcome_concreteness == CheckResult.FAIL,
    )

    # C9
    checks.C9_runtime_applicability_present, issues = check_c9_runtime_applicability(body, issue_kind)
    # warn は blocking_issues に追加しない
    if checks.C9_runtime_applicability_present in (CheckResult.FAIL, CheckResult.LEGACY_MISSING):
        result.blocking_issues.extend(issues)
        _append_findings(
            result,
            issues,
            deterministic_domain_key="runtime_applicability",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
            blocking=True,
            checker_evidence=(
                _make_self_checker_evidence(
                    result,
                    rule_id="C9_runtime_applicability_present",
                    category="runtime_applicability",
                )
                if issues
                else []
            ),
            reviewer_blocker_code="C9",
        )
    elif checks.C9_runtime_applicability_present == CheckResult.WARN:
        for msg in issues:
            _add_warning(
                result,
                code="c9_runtime_applicability_missing",
                severity="warning",
                evidence=[msg],
                suggested_action=(
                    "implementation 以外の Issue でも `## Runtime Verification Applicability`"
                    " セクションの追加を推奨する"
                ),
                emit_finding=False,
            )
        _append_findings(
            result,
            issues,
            deterministic_domain_key="runtime_applicability",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
            blocking=False,
        )

    # C10
    checks.C10_deferred_destination_present, issues = check_c10_deferred_destination(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="deferred_destination",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C10_deferred_destination_present == CheckResult.FAIL,
    )

    # C11
    checks.C11_decision_tag_consistency, issues = check_c11_decision_tag_consistency(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="decision_tag_consistency",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C11_decision_tag_consistency == CheckResult.FAIL,
    )

    # C12: Product trace fields structure
    checks.C12_product_trace_fields_structure, issues = check_c12_product_trace_fields_structure(body)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="product_trace_fields_structure",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C12_product_trace_fields_structure != CheckResult.PASS,
    )

    # C13: VC preflight decision consistency (if JSON provided)
    (
        checks.C13_vc_preflight_decision_consistency,
        issues,
    ) = check_c13_vc_preflight_decision_consistency(vc_preflight_json_path)
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="vc_preflight_decision_consistency",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C13_vc_preflight_decision_consistency != CheckResult.PASS,
    )

    # C14: Extension surface risk-trigger policy (Issue #2290)
    checks.C14_extension_surface_risk_trigger, issues = check_c14_extension_surface_risk_trigger(
        body, issue_kind
    )
    result.blocking_issues.extend(issues)
    _append_findings(
        result,
        issues,
        deterministic_domain_key="extension_surface_risk_trigger",
        finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
        blocking=checks.C14_extension_surface_risk_trigger == CheckResult.FAIL,
    )

    # C15: Runtime Verification profile assertion binding coverage (Issue #2771)
    checks.C15_runtime_assertion_binding_coverage, issues = check_c15_runtime_assertion_binding_coverage(
        body, issue_kind
    )
    if checks.C15_runtime_assertion_binding_coverage == CheckResult.FAIL:
        result.blocking_issues.extend(issues)
        _append_findings(
            result,
            issues,
            deterministic_domain_key="runtime_assertion_binding_coverage",
            finding_kind=REVIEW_ISSUE_FINDING_KIND_HEURISTIC_CONCERN,
            blocking=True,
        )
    elif checks.C15_runtime_assertion_binding_coverage == CheckResult.WARN:
        for msg in issues:
            _add_warning(
                result,
                code="c15_runtime_assertion_binding_coverage_policy_unavailable",
                severity="warning",
                evidence=[msg],
                suggested_action=(
                    "The extension-surface runtime policy's verification_profiles could not be "
                    "evaluated for this Issue's runtime assertion binding coverage (policy "
                    "integrity failure, not an Issue-author-fixable defect). Escalate to a "
                    "human/owner to repair docs/dev/extension-surface-runtime-policy.yaml."
                ),
                emit_finding=False,
            )

    # C15b: disposition classification carrier (Issue #2852 AC9). Non-blocking:
    # emitted only when C15 passed, `emit_finding=False` so the finding count /
    # heuristic_concern_count do not grow, and it never touches checks.* /
    # all_check_values / verdict.
    if checks.C15_runtime_assertion_binding_coverage == CheckResult.PASS:
        disposition_carrier_lines = get_runtime_assertion_disposition_carrier(body, issue_kind)
        if disposition_carrier_lines:
            _add_warning(
                result,
                code=_RUNTIME_ASSERTION_DISPOSITION_CARRIER_CODE,
                severity="advisory",
                evidence=disposition_carrier_lines,
                suggested_action=(
                    "Non-blocking: runtime_assertion_bindings carries an explicit disposition "
                    "(dispositive / non_dispositive_readiness_compat / not_applicable). This is a "
                    "structural classification only, not runtime verification PASS evidence; "
                    "semantic sufficiency of each reason / demonstrated_by stays with semantic review."
                ),
                emit_finding=False,
            )

    # C14c: applied comment-only exemption carrier (Issue #2961). Non-blocking
    # transcription of the shared evaluator's `issue_time_exemptions`;
    # `emit_finding=False` so it never touches checks.* / findings / verdict.
    exemption_carrier_lines = get_extension_surface_issue_time_exemption_carrier(body, issue_kind)
    if exemption_carrier_lines:
        _add_warning(
            result,
            code=_EXTENSION_SURFACE_ISSUE_TIME_EXEMPTION_CARRIER_CODE,
            severity="advisory",
            evidence=exemption_carrier_lines,
            suggested_action=(
                "Non-blocking: a comment-only exemption declaration "
                "(executable_semantics_unchanged) was structurally accepted. The matcher verifies "
                "structure only; that executable semantics are actually unchanged is evidenced by "
                "the PR-time VC execution and review."
            ),
            emit_finding=False,
        )

    # C14b: Extension surface candidate perimeter advisory (Issue #2339,
    # non-blocking -- must never touch checks.* / all_check_values / verdict).
    extension_surface_candidate_advisories = get_extension_surface_candidate_advisories(body, issue_kind)
    if extension_surface_candidate_advisories:
        _add_warning(
            result,
            code="extension_surface_candidate_perimeter_advisory",
            severity="advisory",
            evidence=extension_surface_candidate_advisories,
            suggested_action=(
                "Non-blocking: one or more declared Allowed Paths match a project-local "
                "extension candidate perimeter (unknown_surface_policy.project_candidate_path_globs) "
                "but no known extension-surface risk-trigger rule. Confirm whether this is a "
                "genuine new extension surface; no action required to proceed."
            ),
        )

    # Non-blocking warnings（structured: code/severity/evidence/suggested_action）
    detect_warning_scope_cvs_in_scope_mismatch(body, result)
    detect_warning_vc_untracked_false_negative(body, result)
    detect_warning_vc_negative_grep_without_literal_inventory(body, result)
    _apply_product_spec_check(result, body=body, body_file_path=body_file_path)

    # Verdict
    all_check_values = [
        checks.C1_required_sections,
        checks.C2_stop_conditions_6,
        checks.C3_ac_checkbox_format,
        checks.C4_vc_commands_present,
        checks.C5_ac_vc_number_alignment,
        checks.C6_no_subjective_phrasing,
        checks.C7_required_skills_semantics,
        checks.C8_outcome_concreteness,
        checks.C9_runtime_applicability_present,
        checks.C10_deferred_destination_present,
        checks.C11_decision_tag_consistency,
        checks.C12_product_trace_fields_structure,
        checks.C13_vc_preflight_decision_consistency,
        checks.C14_extension_surface_risk_trigger,
        checks.C15_runtime_assertion_binding_coverage,
    ]
    has_fail = any(v in (CheckResult.FAIL, CheckResult.LEGACY_MISSING) for v in all_check_values)
    has_structured_blockers = bool(result.structured_blockers)
    result.status = _validate_status("failed" if has_fail or has_structured_blockers else "ok")
    result.verdict = "needs-fix" if has_fail or has_structured_blockers else "approve"

    return result


def fetch_issue_body(issue_number: int, repo: str) -> tuple[str, str, str]:
    """Fetch issue body, labels, and title from GitHub.

    Uses plain --json output (parsed as JSON) rather than --jq string
    concatenation: gh's --jq raw-string rendering appends its own trailing
    newline to the output, which previously landed inside the extracted body
    unless stripped -- and stripping silently diverged body_sha256 from the
    canonical raw-body hash used by contract_readiness_check.py /
    run_refinement_preflight.py, causing spurious body_sha_mismatch in
    reviewer_claim_replay.
    """
    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "title,body,labels",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: gh issue view failed: {result.stderr}", file=sys.stderr)
        sys.exit(2)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: gh issue view returned invalid JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(payload, dict):
        print("ERROR: gh issue view returned non-object JSON", file=sys.stderr)
        sys.exit(2)

    raw_labels = payload.get("labels") or []
    if not isinstance(raw_labels, list):
        print("ERROR: gh issue view labels field is not a list", file=sys.stderr)
        sys.exit(2)

    title = payload.get("title") or ""
    body = payload.get("body") or ""
    labels = ",".join(label.get("name", "") for label in raw_labels)

    return body, labels, title


def load_fixture_file(path: str) -> tuple[str, str, str]:
    """Load a fixture file (Markdown with optional YAML-like header)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    labels = ""
    title = ""
    body = content

    # Parse simple header if present
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            header = content[4:end]
            body = content[end + 5:].strip()
            for line in header.splitlines():
                if line.startswith("LABELS:"):
                    labels = line[len("LABELS:"):].strip()
                elif line.startswith("TITLE:"):
                    title = line[len("TITLE:"):].strip()

    return body, labels, title


def result_to_dict(result: CheckerResult) -> dict:
    """Convert CheckerResult to a dict for JSON output."""
    import datetime

    def _serialize_parsed_vc_cmd(cmd: ParsedVcCommand) -> dict:
        return {
            "command": cmd.command,
            "preflight_scope": cmd.preflight_scope.value if cmd.preflight_scope is not None else None,
            "trivially_pass_reason": cmd.trivially_pass_reason,
            "classification": cmd.classification,
            "skip_reason_type": cmd.skip_reason_type,
        }

    payload = {
        "schema": result.schema,
        "schema_version": result.schema_version,
        "verdict": result.verdict,
        "status": result.status,
        "body_sha256": result.body_sha256,
        "issue_kind": result.issue_kind,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deterministic_checks": {
            "C1_required_sections": result.deterministic_checks.C1_required_sections,
            "C2_stop_conditions_6": result.deterministic_checks.C2_stop_conditions_6,
            "C3_ac_checkbox_format": result.deterministic_checks.C3_ac_checkbox_format,
            "C4_vc_commands_present": result.deterministic_checks.C4_vc_commands_present,
            "C5_ac_vc_number_alignment": result.deterministic_checks.C5_ac_vc_number_alignment,
            "C6_no_subjective_phrasing": result.deterministic_checks.C6_no_subjective_phrasing,
            "C7_required_skills_semantics": result.deterministic_checks.C7_required_skills_semantics,
            "C8_outcome_concreteness": result.deterministic_checks.C8_outcome_concreteness,
            "C9_runtime_applicability_present": result.deterministic_checks.C9_runtime_applicability_present,
            "C10_deferred_destination_present": result.deterministic_checks.C10_deferred_destination_present,
            "C11_decision_tag_consistency": result.deterministic_checks.C11_decision_tag_consistency,
            "C12_product_trace_fields_structure": result.deterministic_checks.C12_product_trace_fields_structure,
            "C13_vc_preflight_decision_consistency": result.deterministic_checks.C13_vc_preflight_decision_consistency,
        },
        "blocking_issues": result.blocking_issues,
        "structured_blockers": result.structured_blockers,
        "non_blocking_improvements": result.non_blocking_improvements,
        "findings": result.findings,
        "diff_proposal": result.diff_proposal,
        "parsed_vc_commands": [_serialize_parsed_vc_cmd(c) for c in result.parsed_vc_commands],
    }
    _validate_review_issue_result_payload(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="C1〜C13 決定論的チェッカー — Issue 本文を機械的に検査する"
    )
    parser.add_argument(
        "--mode",
        choices=["check", "merge_readiness"],
        default="check",
        help=(
            "check（既定）: Issue 本文を C1〜C13 で判定する。"
            "merge_readiness: ISSUE_CONTRACT_READINESS_RESULT_V1 を "
            "REVIEW_ISSUE_RESULT_V1 へ決定論的に合成する（Issue #1791）。"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--file", "-f", help="フィクスチャファイルパス（テスト用）")
    group.add_argument("--issue", "-i", type=int, help="GitHub Issue 番号")
    parser.add_argument("--repo", "-r", help="GitHub repo (owner/repo)。--issue と共に使用")
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "JSON 出力モード（stdout = JSON only / stderr = diagnostics）。"
            "caller は stdout のみを parse し stderr を stdout に merge しないこと。"
        ),
    )
    parser.add_argument("--vc-preflight-json", help="VC preflight JSON path for C13 check")
    parser.add_argument(
        "--review-result-file",
        help="merge_readiness mode: REVIEW_ISSUE_RESULT_V1 JSON ファイルパス",
    )
    parser.add_argument(
        "--readiness-result-file",
        help="merge_readiness mode: ISSUE_CONTRACT_READINESS_RESULT_V1 JSON ファイルパス",
    )
    parser.add_argument(
        "--readiness-artifact-path",
        help="merge_readiness mode: checker_evidence.artifact_path に記録する readiness artifact の実パス",
    )
    parser.add_argument(
        "--iteration-id",
        help="merge_readiness mode: checker_evidence.iteration_id に記録する iteration 識別子",
    )
    parser.add_argument(
        "--output-file",
        help="merge_readiness mode: 合成後の REVIEW_ISSUE_RESULT_V1 出力先（省略時は stdout）",
    )
    args = parser.parse_args()

    if args.mode == "merge_readiness":
        missing = [
            flag
            for flag, value in (
                ("--review-result-file", args.review_result_file),
                ("--readiness-result-file", args.readiness_result_file),
                ("--readiness-artifact-path", args.readiness_artifact_path),
                ("--iteration-id", args.iteration_id),
            )
            if not value
        ]
        if missing:
            print(
                "ERROR: --mode merge_readiness には "
                f"{', '.join(missing)} が全て必要です",
                file=sys.stderr,
            )
            sys.exit(2)

        review_result = json.loads(
            Path(args.review_result_file).read_text(encoding="utf-8")
        )
        readiness_result = json.loads(
            Path(args.readiness_result_file).read_text(encoding="utf-8")
        )
        try:
            merged = merge_readiness_into_review_result(
                review_result,
                readiness_result,
                readiness_artifact_path=args.readiness_artifact_path,
                iteration_id=args.iteration_id,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

        output_text = json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output_file:
            Path(args.output_file).write_text(output_text + "\n", encoding="utf-8")
        else:
            print(output_text)
        sys.exit(0 if merged.get("verdict") == "approve" else 1)

    if not args.file and not args.issue:
        print(
            "ERROR: --mode check には --file または --issue が必要です",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.issue and not args.repo:
        print("ERROR: --issue には --repo が必要です", file=sys.stderr)
        sys.exit(2)

    if args.issue:
        # --repo is bound to the single identity config (fail-closed) before any gh call.
        _repo_identity = _load_repo_identity()
        try:
            trusted = _repo_identity.is_trusted_repo(args.repo)
        except _repo_identity.RepositoryIdentityError as exc:
            print(f"ERROR: repository identity unavailable: {exc}", file=sys.stderr)
            sys.exit(2)
        if not trusted:
            print(f"ERROR: repository identity mismatch: --repo {args.repo!r}", file=sys.stderr)
            sys.exit(2)

    if args.json:
        # In --json mode: redirect stdout to stderr during fetch/check so that any
        # incidental stdout (e.g. from sub-libraries) does not pollute the JSON output.
        with contextlib.redirect_stdout(sys.stderr):
            if args.file:
                body, labels, title = load_fixture_file(args.file)
            else:
                body, labels, title = fetch_issue_body(args.issue, args.repo)
            result = run_checks(
                body,
                labels,
                title,
                args.vc_preflight_json,
                body_file_path=args.file,
            )
            output = result_to_dict(result)
        # Emit JSON exclusively to stdout (after redirect is restored)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        if args.file:
            body, labels, title = load_fixture_file(args.file)
        else:
            body, labels, title = fetch_issue_body(args.issue, args.repo)
        result = run_checks(
            body,
            labels,
            title,
            args.vc_preflight_json,
            body_file_path=args.file,
        )
        output = result_to_dict(result)
        print(f"verdict: {result.verdict}")
        print(f"issue_kind: {result.issue_kind}")
        print()
        print("deterministic_checks:")
        for key, val in output["deterministic_checks"].items():
            print(f"  {key}: {val}")
        if result.blocking_issues:
            print()
            print("blocking_issues:")
            for issue in result.blocking_issues:
                print(f"  - {issue}")
        if result.non_blocking_improvements:
            print()
            print("non_blocking_improvements:")
            for improvement in result.non_blocking_improvements:
                print(f"  - {improvement}")

    # Exit code: 0 = approve, 1 = needs-fix
    sys.exit(0 if result.verdict == "approve" else 1)


if __name__ == "__main__":
    main()
