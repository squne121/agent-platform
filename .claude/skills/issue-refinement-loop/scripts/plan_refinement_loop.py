#!/usr/bin/env python3
"""
plan_refinement_loop.py

Read-only CLI that analyzes an Issue body, comments, and known_context
to produce a REFINEMENT_LOOP_PLAN_V1 JSON payload describing policy decisions
and evidence extraction.

Usage:
    python3 plan_refinement_loop.py < input.json

Input (stdin): REFINEMENT_LOOP_PLANNER_INPUT_V1 JSON
Output (stdout): REFINEMENT_LOOP_PLAN_V1 JSON
Logs (stderr): diagnostic messages

Exit codes:
    0 - success (may include fail_closed.required=true)
    2 - invalid input schema
    3 - internal error
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_IMPL_MAIN_DRIFT_SCRIPTS = Path(__file__).resolve().parents[4] / "scripts" / "agent-ops"
if str(_IMPL_MAIN_DRIFT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_IMPL_MAIN_DRIFT_SCRIPTS))
try:
    from main_drift_policy import classify_main_drift
except ImportError:  # pragma: no cover
    classify_main_drift = None


def classify_refinement_evidence_epoch(context: dict[str, Any]) -> dict[str, Any]:
    """Classify evidence only; refinement retains Issue/snapshot mutation."""
    if classify_main_drift is None:
        return {"route": "hard_stop", "reason_code": "main_drift_policy_import_failed"}
    try:
        result = classify_main_drift(
            current_base_sha=context["current_base_sha"],
            evidence_base_sha=context["evidence_base_sha"],
            allowed_paths_snapshot_base_sha=context[
                "allowed_paths_snapshot_base_sha"
            ],
            allowed_paths=context["allowed_paths"],
            latest_main_net_diff=context.get("latest_main_net_diff", ()),
            expected_head_sha=context["expected_head_sha"],
            observed_head_sha=context["observed_head_sha"],
            expected_old_sha=context["expected_old_sha"],
            observed_old_sha=context["observed_old_sha"],
            semantic_ambiguity=bool(context.get("semantic_ambiguity", False)),
            owner="refinement",
        )
    except (KeyError, TypeError):
        return {"route": "hard_stop", "reason_code": "main_drift_context_invalid"}
    return {
        "route": result.route,
        "reason_code": result.reason_code,
        "evidence_epoch": dict(result.evidence_epoch or {}),
        "reverify": dict(result.reverify),
        "reusable_evidence": dict(result.reusable_evidence),
        "mutation_owner": "refinement",
    }


# ---------------------------------------------------------------------------
# known_context["main_drift"] production contract (Issue #2102 fix_delta,
# P0-C: the previous revision left this key's producer undocumented, making
# classify_refinement_evidence_epoch() / _refinement_main_drift_decision()
# unreachable in production).
#
# This planner is read-only and performs no git/GitHub I/O itself (see the
# module docstring). known_context.main_drift is therefore never
# self-populated -- it MUST be constructed by the orchestrator step that
# invokes this script (the issue-refinement-loop control-plane step that
# runs BEFORE issuing REFINEMENT_LOOP_PLANNER_INPUT_V1 to stdin), using the
# same live-readback boundary already established for
# known_context.scope_delta_decision (see the "唯一の production 呼び出し元"
# / "診断用の echo であり canonical な mutation-phase routing は...consumer
# 境界が担う" pattern in issue-refinement-loop/SKILL.md's Step 4 scope-reframe
# section): this planner's `decisions.main_drift_evidence_epoch` output is an
# ADVISORY diagnostic echo, not canonical mutation-phase routing authority.
# The actual rebind/reverify mutation (fresh contract-snapshot
# materialization, stale-evidence invalidation) is performed by the
# refinement-loop consumer boundary that reads this echo, not by this
# planner.
#
# Before invoking this script, the orchestrator must populate
# known_context["main_drift"] as a dict with the following keys (all 40-hex
# SHAs unless noted):
#   current_base_sha                 live default-branch HEAD, read the same
#                                     way materialize_issue_scope_snapshot.py
#                                     ::_live_default_branch() reads it (a
#                                     GitHub REST call, not a local ref).
#   evidence_base_sha                base SHA the existing Contract Snapshot
#                                     / scope snapshot is bound to.
#   allowed_paths_snapshot_base_sha  base SHA the current Allowed Paths
#                                     snapshot is bound to.
#   allowed_paths                    list[str], from the live Issue body.
#   latest_main_net_diff             list[str], `git diff --name-only
#                                     evidence_base_sha current_base_sha`
#                                     (main-drift-touched paths; this is
#                                     DISTINCT from a final post-reconciliation
#                                     net diff, which is the refinement/impl
#                                     mutation consumer's responsibility, not
#                                     this planner's).
#   expected_old_sha / observed_old_sha   CAS pair for whatever ref this
#                                     evidence rebind will ultimately mutate.
#   semantic_ambiguity (optional)    bool; SHOULD be derived from an actual
#                                     `git merge-tree --write-tree` conflict
#                                     check by the orchestrator, not asserted
#                                     freehand (see route_loop_verdict_v2.py /
#                                     pr_head_replay_publish_exec.py for the
#                                     canonical deterministic oracle used on
#                                     the implementation-loop side).
# If known_context.main_drift is omitted, this planner emits no
# main_drift_evidence_epoch decision (the pre-#2102 behavior is preserved
# for callers that do not yet supply main-drift facts).
#
# Current production-wiring status (honest disclosure, fix_delta iteration
# 3): as of this revision, no production Python code path actually calls
# run_refinement_preflight.py's orchestrator step to assemble
# known_context["main_drift"] and pass it into this planner. The above
# contract therefore documents a MUST requirement that the control-plane
# has not yet implemented as automated wiring -- known_context.main_drift
# remains dead code in production today (this file emits no
# main_drift_evidence_epoch decision on any live invocation). Wiring the
# orchestrator step described above (live GitHub REST readback, git diff,
# and a git merge-tree --write-tree conflict check for semantic_ambiguity)
# is tracked as follow-up work outside this Issue's bounded scope; the
# `semantic_ambiguity` derivation the contract requires here mirrors the
# same deterministic oracle already implemented in
# pr_head_replay_publish_exec.py::_merge_tree_conflicts() on the
# implementation-loop side (exit code from `git merge-tree --write-tree`,
# non-zero == conflict), not a caller-asserted boolean.
# ---------------------------------------------------------------------------


def _refinement_main_drift_decision(
    known_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Project optional live main-drift facts into the planner's SSOT output.

    See the known_context["main_drift"] production contract comment above
    this function for who populates this key and when.
    """
    if not known_context or "main_drift" not in known_context:
        return None
    context = known_context["main_drift"]
    if not isinstance(context, dict):
        return {"route": "hard_stop", "reason_code": "main_drift_context_invalid"}
    return classify_refinement_evidence_epoch(context)

try:
    import yaml as _yaml_module
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

try:
    from scope_signal_delta import (
        classify_scope_delta_authority,
        compute_scope_signal_delta,
        extract_sections as delta_extract_sections,
        iter_section_lines as delta_iter_section_lines,
    )
except ImportError:  # pragma: no cover - subprocess/CLI fallback path
    compute_scope_signal_delta = None
    classify_scope_delta_authority = None
    delta_extract_sections = None
    delta_iter_section_lines = None

# #2048 regression follow-up (#1877/PR #1884): wire the rewrite loop's
# scope-reframe / empty-operations router into the planner's own output so
# an approved trusted-anchor scope reframe with an empty derived
# operations[] is echoed here consistently with
# scope_signal_delta.run_trusted_anchor_iteration_zero()'s production
# annotation (see `_compute_scope_reframe_rewrite_route` below). This is
# opt-in: it only activates when the caller supplies
# `known_context.scope_delta_decision.operations`, so existing fixtures that
# never set that key keep a byte-identical `decisions.scope_delta_decision`
# echo.
try:
    from decide_rewrite_route import (
        SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1,
        decide_scope_reframe_contract_route,
        classify_scope_reframe_disposition,
    )
    _SCOPE_REFRAME_ROUTER_IMPORT_FAILED = False
except ImportError:  # pragma: no cover - subprocess/CLI fallback path
    SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1 = None
    decide_scope_reframe_contract_route = None
    classify_scope_reframe_disposition = None
    # PR #2057 OWNER review P2: a safety-critical routing dependency import
    # failure must be a diagnosable reason code on the planner echo, not a
    # silently vanished sidecar (`_compute_scope_reframe_rewrite_route()`
    # returns a `{"disposition": "invalid", "reason_code":
    # "scope_reframe_router_import_failed", ...}` sidecar instead of `None`
    # whenever the caller actually supplied a scope_delta_decision with an
    # `operations` key -- `None` is reserved for "this caller never
    # opted in", not "the router could not be classified").
    _SCOPE_REFRAME_ROUTER_IMPORT_FAILED = True

# #2048 AC4 naming-collision note: route_after_rewrite.py's
# RouteResult.implementation_allowed (gated on post-mutation body/title
# readback + a fresh review/preflight run) is a DIFFERENT field from
# scope_signal_delta.SCOPE_DELTA_AUTHORITY_V1's nested
# `route.implementation_allowed` (whether a trusted-anchor directive itself
# authorizes implementation). To keep that distinction explicit at the one
# place this planner echoes rewrite-router state, we import
# route_after_rewrite.compute_implementation_allowed under a
# collision-avoiding alias and expose its result (when the caller supplies
# the readback signals) as `rewrite_router_implementation_allowed` — never
# as a bare `implementation_allowed` key that could be confused with the
# scope_delta_authority field.
try:
    from route_after_rewrite import (
        compute_implementation_allowed as _rewrite_router_compute_implementation_allowed,
    )
except ImportError:  # pragma: no cover - subprocess/CLI fallback path
    _rewrite_router_compute_implementation_allowed = None


@dataclass(frozen=True)
class SourceLine:
    number: int
    text: str


class ScopeSignalDeltaError(RuntimeError):
    """Raised when scope_signal_delta_input exists but cannot be consumed safely."""

    def __init__(self, message: str, reason_code: str = "ambiguous_scope_signal"):
        super().__init__(message)
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "refinement_loop_plan/v1"

# Confidence levels
CONFIDENCE_DETERMINISTIC = "deterministic"
CONFIDENCE_UNKNOWN = "unknown"

# Reason codes
INVESTIGATION_REASON_TARGET_PATHS_PRESENT = "target_paths_present"
INVESTIGATION_REASON_REPO_FACT_CLAIM = "repo_fact_claim_in_outcome_or_ac_or_vc"
INVESTIGATION_REASON_SAME_FILE_ROLLUP = "same_file_scope_rollup_conflict"
INVESTIGATION_REASON_REVIEWER_REQUESTED = "reviewer_requested_repo_fact_check"
INVESTIGATION_REASON_ANCHOR_COMMENT = "anchor_comment_requires_fact_check"
INVESTIGATION_REASON_NO_REPO_FACT = "no_repo_fact_claim"
INVESTIGATION_REASON_UNKNOWN_SCHEMA = "unknown_input_schema"

WEB_RESEARCH_REASON_EXTERNAL_SPEC = "critical_external_spec_claim"
WEB_RESEARCH_REASON_HUMAN_REQUESTED = "human_requested_web_verification"
WEB_RESEARCH_REASON_CLI_API_BEHAVIOR = "current_cli_api_auth_or_migration_behavior"
WEB_RESEARCH_REASON_NO_CLAIM = "no_critical_external_claim"
WEB_RESEARCH_REASON_UNKNOWN_SCHEMA = "unknown_input_schema"

# External claim role is intentionally a small decision-dependency marker,
# not a second evidence ledger.  The planner has only Issue text at this
# point, so it defaults to fail-closed ``dispositive``.  A later consumer may
# use ``non_dispositive`` only when the codebase investigation has established
# that repository evidence independently determines the requested decision.
EXTERNAL_CLAIM_ROLE_DISPOSITIVE = "dispositive"
EXTERNAL_CLAIM_ROLE_NON_DISPOSITIVE = "non_dispositive"

SCOPE_SIGNAL_REASON_NEW_IN_SCOPE = "new_in_scope_area"
SCOPE_SIGNAL_REASON_NEW_PATH_LAYER = "new_allowed_path_layer"
SCOPE_SIGNAL_REASON_NEW_UNVERIFIABLE_AC = "new_unverifiable_ac"
SCOPE_SIGNAL_REASON_ANCHOR_REFRAME = "anchor_reframe_exclusion"
SCOPE_SIGNAL_REASON_NO_SIGNAL = "no_scope_signal"
SCOPE_SIGNAL_REASON_MISSING_DELTA_INPUT = "missing_scope_signal_delta_input"

_HARD_STOP_SCOPE_SIGNAL_PHASES = frozenset({"preflight", "post_rewrite_check", "decide_next_action"})

# ---------------------------------------------------------------------------
# SCOPE_SIGNAL_GUARD_DECISION_V2 (#1090) -- escalation lane split
# ---------------------------------------------------------------------------

PATH_LAYER_RUNTIME = "runtime"
PATH_LAYER_DOCS = "docs"
PATH_LAYER_SKILL = "skill"
PATH_LAYER_HOOK = "hook"
PATH_LAYER_AGENT = "agent"
PATH_LAYER_TEST_FIXTURE = "test_fixture"
PATH_LAYER_UNKNOWN = "unknown"

SCOPE_ROUTE_PROCEED_WITH_NOTES = "proceed_with_notes"
SCOPE_ROUTE_HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"
SCOPE_ROUTE_SECURITY_RISK_GATE_REQUIRED = "security_risk_gate_required"
SCOPE_ROUTE_INVALID_SCOPE_DELTA_APPROVAL = "invalid_scope_delta_approval"
SCOPE_ROUTE_NOT_TRIGGERED = "not_triggered"

TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# #558 owns the security gate itself; this is a narrow deterministic
# fail-closed check applied regardless of scope delta approval.
# The path/term vocabulary mirrors the real-security-risk examples listed
# in #558 (secrets, tokens, credentials, auth / access-control surfaces,
# CI supply-chain paths) without implementing the gate itself.
SECURITY_SENSITIVE_PATH_PREFIXES = (
    ".claude/hooks/",
    ".claude/agents/",
    ".github/workflows/",
    ".github/actions/",
    ".github/dependabot.yml",
    ".github/CODEOWNERS",
    "docs/dev/secret-policy.md",
)
# Word-boundary regex (not raw substring) so that e.g. "author" does not
# match "auth" and "tokenizer" does not match "token".
_SECURITY_SENSITIVE_TERM_RE = re.compile(
    r"(?<![a-z0-9])("
    r"secret|token|permission|credential|auth|oidc"
    r"|access[-_]?control|deploy[-_]?key|private[-_]?key"
    r")(?![a-z0-9])"
)

SUGGESTED_CONTRACT_PATCH_TEMPLATE = (
    "OWNER/MEMBER/COLLABORATOR being a trusted author must post an "
    "ANCHOR_SCOPE_REFRAME (or `Scope Delta Approval` / "
    "`Allowed Paths Expansion Rationale`) comment on this issue. "
    "See references/scope-signal-guard.md."
)

FAIL_CLOSED_REASON_MALFORMED_CONTRACT = "malformed_machine_readable_contract"
FAIL_CLOSED_REASON_MISSING_SECTION = "missing_required_section"
FAIL_CLOSED_REASON_MISSING_PARENT_SECTION = "missing_required_parent_section"
FAIL_CLOSED_REASON_AMBIGUOUS_SIGNAL = "ambiguous_scope_signal"
FAIL_CLOSED_REASON_UNKNOWN_SCHEMA = "unknown_input_schema"
FAIL_CLOSED_REASON_INTERNAL_ERROR = "planner_internal_error"
FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE = "template_required_sections_unavailable"
FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND = "unknown_issue_kind"
FAIL_CLOSED_REASON_MISSING_CONTRACT_KEY = "missing_required_contract_key"
FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR = "contract_schema_parse_error"
FAIL_CLOSED_REASON_ISSUE_KIND_POLICY_LOAD_ERROR = "issue_kind_policy_load_error"
FAIL_CLOSED_REASON_CHECKER_INTERNAL_ERROR = "checker_internal_error"
FAIL_CLOSED_REASON_TEMPLATE_RESOLUTION_ERROR = "template_resolution_error"

# Override policy: which fail_closed reason codes can be overridden by human_decision_reframe
# AC7: missing_required_section and missing_required_contract_key only
# unknown_issue_kind, issue_kind_policy_load_error, contract_schema_parse_error,
# template_resolution_error, checker_internal_error are never overridable.
OVERRIDE_POLICY = {
    "allowed_reason_codes": [
        FAIL_CLOSED_REASON_MISSING_SECTION,
        FAIL_CLOSED_REASON_MISSING_CONTRACT_KEY,
    ],
    "never_override_reason_codes": [
        FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND,
        FAIL_CLOSED_REASON_ISSUE_KIND_POLICY_LOAD_ERROR,
        FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR,
        FAIL_CLOSED_REASON_TEMPLATE_RESOLUTION_ERROR,
        FAIL_CLOSED_REASON_CHECKER_INTERNAL_ERROR,
    ],
}

# Required machine-readable contract keys that must be present when a YAML contract exists
REQUIRED_CONTRACT_KEYS = [
    "contract_schema_version",
    "issue_kind",
]

# Standard required sections for implementation issues
STANDARD_REQUIRED_SECTIONS = [
    "Outcome",
    "Acceptance Criteria",
    "Verification Commands",
    "Allowed Paths",
]

# ---------------------------------------------------------------------------
# ISSUE_KIND_POLICY_V1 SSOT loader
# ---------------------------------------------------------------------------
# Canonical source: docs/dev/github-ops.md ## ISSUE_KIND_POLICY_V1
# Local ISSUE_KIND_ALLOWLIST definition is prohibited (SSOT single-source rule).
# Consumer MUST call _get_issue_kind_allowlist() to get the allowlist.

_ISSUE_KIND_POLICY_CACHE: "dict | None" = None


class IssueKindPolicyLoadError(RuntimeError):
    """Raised when ISSUE_KIND_POLICY_V1 cannot be loaded from SSOT.

    Fail-closed: callers must not silently substitute a hardcoded fallback.
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
        repo_root = _find_repo_root()

    ssot_path = repo_root / "docs" / "dev" / "github-ops.md"
    if not ssot_path.exists():
        raise IssueKindPolicyLoadError(
            f"SSOT file not found: {ssot_path}. "
            "Cannot load ISSUE_KIND_POLICY_V1 — fail-closed."
        )

    if not _YAML_AVAILABLE:
        raise IssueKindPolicyLoadError(
            "PyYAML is not available; cannot parse ISSUE_KIND_POLICY_V1."
        )

    try:
        text = ssot_path.read_text(encoding="utf-8")
        # Extract the ISSUE_KIND_POLICY_V1 YAML block
        match = re.search(r"```yaml\s*\nISSUE_KIND_POLICY_V1:(.*?)```", text, re.DOTALL)
        if not match:
            raise IssueKindPolicyLoadError(
                f"ISSUE_KIND_POLICY_V1 fenced YAML block not found in {ssot_path}. "
                "Ensure the block starts with ```yaml on a line followed by 'ISSUE_KIND_POLICY_V1:'."
            )

        yaml_content = "ISSUE_KIND_POLICY_V1:" + match.group(1)
        parsed = _yaml_module.safe_load(yaml_content)
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


def _get_issue_kind_allowlist() -> frozenset:
    """Return the canonical_kinds frozenset from SSOT (docs/dev/github-ops.md).

    Raises IssueKindPolicyLoadError if SSOT cannot be loaded.
    """
    return _load_issue_kind_policy()["canonical_kinds"]


def _normalize_issue_kind(kind: str) -> "str | None":
    """Normalize an issue_kind string against SSOT canonical_kinds and aliases.

    Returns:
      - The canonical kind if ``kind`` is already canonical.
      - The alias target if ``kind`` is in SSOT aliases (e.g. "design" → "research").
      - None if ``kind`` is unknown (not canonical and not an alias).
      - None if the SSOT cannot be loaded (IssueKindPolicyLoadError).

    Callers should treat None as FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND.
    """
    try:
        policy = _load_issue_kind_policy()
    except IssueKindPolicyLoadError:
        return None

    canonical_kinds = policy["canonical_kinds"]
    aliases = policy["aliases"]

    if kind in canonical_kinds:
        return kind
    if kind in aliases:
        target = aliases[kind]
        # Only return the alias target if it is itself canonical
        if target in canonical_kinds:
            return target
    return None


def _clear_issue_kind_policy_cache() -> None:
    """Clear the SSOT cache (for testing only)."""
    global _ISSUE_KIND_POLICY_CACHE
    _ISSUE_KIND_POLICY_CACHE = None


# ---------------------------------------------------------------------------
# Template load result
# ---------------------------------------------------------------------------


@dataclass
class TemplateLoadResult:
    """Result of loading required section labels from a template file.

    Blocker 2: distinguishes success (error=None) from failure (error=reason_code).
    An empty required_labels with error=None means the template genuinely has 0
    required sections — that is valid. An error means we cannot trust the result.
    """

    required_labels: list[str]
    error: Optional[str]  # None = success; str = fail_closed reason code


# ---------------------------------------------------------------------------
# Repository root resolution
# ---------------------------------------------------------------------------

def _find_repo_root() -> Path:
    """Find repository root by walking up to find .git directory."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume we are in .claude/skills/issue-refinement-loop/scripts/
    return Path(__file__).resolve().parent.parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Path extraction: matches `.claude/`, `docs/`, `scripts/`, `.github/workflows/`, `src/`, `tests/`
# Avoids matching paths inside fenced code blocks (handled separately)
PATH_PATTERN = re.compile(
    r"(?P<path>(?:"
    r"\.claude/[^\s\)`]+|"
    r"docs/[^\s\)`]+|"
    r"scripts/[^\s\)`]+|"
    r"\.github/workflows/[^\s\)`]+|"
    r"src/[^\s\)`]+|"
    r"tests?/[^\s\)`]+"
    r"))"
)

# Markers for unmaterialized child slots
UNMATERIALIZED_MARKER_PATTERN = re.compile(
    r"(?:（未起票）|（未起票）|\(未起票\)|unmaterialized|TBD)"
)

# Keywords indicating critical external claims (B7: more specific keywords to reduce false positives)
CRITICAL_EXTERNAL_KEYWORDS = {
    "official",
    "api",
    "cli",
    "auth",
    "migration",
}

# Keywords for human-requested web verification in comments
HUMAN_WEB_VERIFICATION_KEYWORDS = {
    "webで確認",
    "web確認",
    "verify externally",
    "external verification",
    "公式 docs",
    "official docs",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """Compute SHA256 of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    """Produce canonical JSON (sorted keys, compact, NaN-safe)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------------------
# ISSUE_EXECUTION_DECISION_V1 (#1677): canonical relation-graph decision.
#
# This module is the canonical PRODUCER of ISSUE_EXECUTION_DECISION_V1
# (schemas/issue_execution_decision_v1.schema.json owns the static/closed
# shape; validate_issue_execution_decision.py -- the standalone canonical
# module -- owns the cross-field graph invariants). Other scripts in this
# skill (run_refinement_preflight.py) import validate_issue_execution_decision
# from that module rather than re-implementing the invariants, per the #1677
# Canonical Contract Freeze. #1873: this module no longer self-validates its
# own output before emitting it (see the build_issue_execution_decision call
# site below).
# ---------------------------------------------------------------------------

# PR #1767 owner review (P0-4/AC12 Scope Delta): validate_issue_execution_
# decision.py is the standalone canonical module owning the
# ISSUE_EXECUTION_DECISION_V1 schema-version/downstream-policy constants (and
# semantic validator, consumed independently by run_refinement_preflight.py).
# plan_refinement_loop.py imports the shared constants from it rather than
# defining its own copy. #1873: plan_refinement_loop.py no longer
# self-validates the decision it builds (see build_issue_execution_decision
# call site below), so only the constants are imported here.
try:
    from validate_issue_execution_decision import (
        ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY,
        ISSUE_EXECUTION_DECISION_SCHEMA_VERSION,
    )
except ImportError:  # pragma: no cover - subprocess/CLI fallback path
    ISSUE_EXECUTION_DECISION_SCHEMA_VERSION = "ISSUE_EXECUTION_DECISION_V1"
    ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY = {
        "semantic_reclassification": "forbidden",
        "freshness_validation": "required",
        "stale_action": "rerun_issue_refinement",
    }

# PR #1767 owner review (P0-2): ISSUE_SCOPE_ROLLUP_PLAN_V2 is a coordination
# plan, not a semantic dependency graph. suggested_action /
# ordering_constraint values do NOT carry a confirmed direction or a
# confirmed duplicate/absorb relationship -- only "proceed_with_coordination"
# maps to a genuinely non-committal "coordinates" relation. All other
# suggested_action / ordering_constraint values that previously asserted a
# fabricated direction or relation type (sequential_required -> depends_on,
# merge_into_current_pr/amend_current_issue -> duplicate,
# create_parent_rollup_issue -> absorb) have been removed; they now route to
# an "unclassified" bucket that forces execution.state=deferred instead of
# silently defaulting to selected/duplicate/blocked.
_ROLLUP_ACTION_COORDINATE = frozenset({"proceed_with_coordination"})
_ROLLUP_UNCLASSIFIED_ACTIONS = frozenset(
    {
        "merge_into_current_pr",
        "amend_current_issue",
        "create_parent_rollup_issue",
        "human_review_required",
    }
)
_ROLLUP_UNCLASSIFIED_ORDERINGS = frozenset({"sequential_required"})
_KNOWN_ROLLUP_COMPLETENESS_VALUES = frozenset({"full", "partial"})


def _sha256_prefixed(text: str) -> str:
    """SHA256 digest with the 'sha256:' prefix required by ISSUE_EXECUTION_DECISION_V1."""
    return "sha256:" + _sha256(text)


def _node_body_sha256_for_candidate(candidate: dict[str, Any]) -> str:
    """Deterministic node digest for a scope-rollup candidate (no raw body available)."""
    return _sha256_prefixed(_canonical_json(candidate))


def _derive_execution_decision_inputs(
    target_issue_number: int,
    scope_rollup_result: "dict[str, Any] | None",
) -> tuple[
    list[dict[str, Any]],
    dict[int, str],
    list[int],
    bool,
    bool,
    bool,
    list[int],
    list[str],
]:
    """
    Derive (relations, extra_nodes, predecessors, duplicate_hit,
    issues_complete, dependencies_complete, unresolved_references,
    unclassified_notes) from an ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact (#1677
    AC4 join).

    PR #1767 owner review fixes (P0-1, P0-2):
    - Artifact absence, wrong schema_version, unknown/missing
      input.completeness, and malformed candidates are now treated as
      INCOMPLETE evidence (issues_complete=False), not as "complete ->
      selected". A missing/corrupt/stale artifact must never fail-open into
      an implementation-permitting decision.
    - suggested_action / ordering_constraint values that would have asserted
      a fabricated direction or relation type are routed to
      unclassified_notes instead of becoming depends_on/duplicate/absorb
      relations. Only "proceed_with_coordination" produces a relation
      ("coordinates" -- non-committal, no direction/duplication claim).
    - PR candidates (candidate.kind == "pr") are excluded from the Issue
      node/relation graph entirely (Issue #N and PR #N must not collide as
      the same node).
    """
    relations: list[dict[str, Any]] = []
    extra_nodes: dict[int, str] = {}
    predecessors: list[int] = []
    duplicate_hit = False
    unclassified_notes: list[str] = []

    if not isinstance(scope_rollup_result, dict):
        return (
            relations,
            extra_nodes,
            predecessors,
            duplicate_hit,
            False,
            False,
            [target_issue_number],
            ["scope_rollup_artifact_absent"],
        )

    if scope_rollup_result.get("schema_version") != 2:
        return (
            relations,
            extra_nodes,
            predecessors,
            duplicate_hit,
            False,
            False,
            [target_issue_number],
            ["scope_rollup_artifact_schema_version_mismatch"],
        )

    input_meta = scope_rollup_result.get("input")
    completeness_str = (input_meta or {}).get("completeness") if isinstance(input_meta, dict) else None
    if completeness_str not in _KNOWN_ROLLUP_COMPLETENESS_VALUES:
        return (
            relations,
            extra_nodes,
            predecessors,
            duplicate_hit,
            False,
            False,
            [target_issue_number],
            ["scope_rollup_completeness_unknown_or_missing"],
        )

    issues_complete = completeness_str == "full"
    dependencies_complete = issues_complete
    unresolved_references = [] if issues_complete else [target_issue_number]

    candidates = scope_rollup_result.get("candidates")
    if not isinstance(candidates, list):
        return (
            relations,
            extra_nodes,
            predecessors,
            duplicate_hit,
            False,
            False,
            [target_issue_number],
            ["scope_rollup_candidates_malformed"],
        )

    for candidate in candidates:
        if not isinstance(candidate, dict):
            # Malformed candidate: cannot assert completeness of the
            # collision picture (was previously silently skipped).
            issues_complete = False
            dependencies_complete = False
            if target_issue_number not in unresolved_references:
                unresolved_references.append(target_issue_number)
            unclassified_notes.append("malformed_candidate_entry")
            continue

        number = candidate.get("number")
        kind = candidate.get("kind")
        if not isinstance(number, int) or kind not in ("issue", "pr"):
            issues_complete = False
            dependencies_complete = False
            if target_issue_number not in unresolved_references:
                unresolved_references.append(target_issue_number)
            unclassified_notes.append("malformed_candidate_entry")
            continue

        if kind == "pr":
            # P0-2: PR candidates must not collide with Issue nodes in the
            # Issue relation graph (separate namespace). PR-vs-Issue
            # collision is out of scope for this decision.
            continue

        signals = candidate.get("signals")
        evidence = [f"signal:{s}" for s in signals] if isinstance(signals, list) and signals else [
            f"scope_rollup_candidate:{number}"
        ]

        action = candidate.get("suggested_action")
        ordering = candidate.get("ordering_constraint")

        if action in _ROLLUP_ACTION_COORDINATE:
            relations.append(
                {
                    "source_issue_number": target_issue_number,
                    "target_issue_number": number,
                    "relation_type": "coordinates",
                    "evidence": evidence,
                }
            )
            extra_nodes[number] = _node_body_sha256_for_candidate(candidate)
        elif action in _ROLLUP_UNCLASSIFIED_ACTIONS or ordering in _ROLLUP_UNCLASSIFIED_ORDERINGS:
            # Ambiguous / direction-undetermined coordination signal. Do NOT
            # fabricate a depends_on/duplicate/absorb relation from it.
            unclassified_notes.append(
                f"candidate #{number}: suggested_action={action!r} "
                f"ordering_constraint={ordering!r} requires human "
                f"classification (not auto-converted to a semantic relation)"
            )
        # Any other suggested_action/ordering_constraint combination (e.g.
        # keep_separate_with_reason + parallel_ok) is genuinely resolved:
        # no relation, no unclassified note needed.

    predecessors = sorted(set(predecessors))
    return (
        relations,
        extra_nodes,
        predecessors,
        duplicate_hit,
        issues_complete,
        dependencies_complete,
        unresolved_references,
        unclassified_notes,
    )


def build_issue_execution_decision(
    issue_number: int,
    issue_body_sha256: str,
    generated_at: str,
    known_context: "dict[str, Any] | None",
) -> dict[str, Any]:
    """
    Build an ISSUE_EXECUTION_DECISION_V1 dict (#1677).

    Reads known_context['scope_rollup_result'] (ISSUE_SCOPE_ROLLUP_PLAN_V2,
    joined in by run_refinement_preflight.py) when present. Absent that key,
    emits a minimal 'selected' decision for the target Issue alone.
    """
    scope_rollup_result = None
    if isinstance(known_context, dict):
        scope_rollup_result = known_context.get("scope_rollup_result")

    (
        relations,
        extra_nodes,
        predecessors,
        duplicate_hit,
        issues_complete,
        dependencies_complete,
        unresolved_references,
        unclassified_notes,
    ) = _derive_execution_decision_inputs(issue_number, scope_rollup_result)

    # issue_body_sha256 passed in is already the computed hex digest of the
    # issue body (matches source.issue_body_sha256 elsewhere in this module).
    # BUGFIX (PR #1767 owner review, P0-3.1): the previous code called
    # _sha256_prefixed(issue_body_sha256), which re-hashes the already-hex
    # digest (SHA256(hex(SHA256(body))) instead of SHA256(body)), making
    # target_body_sha256 useless for freshness verification against the live
    # Issue body. A verified hex digest must only be prefixed, never re-hashed.
    target_body_sha256 = (
        issue_body_sha256
        if issue_body_sha256.startswith("sha256:")
        else f"sha256:{issue_body_sha256}"
    )
    nodes_map: dict[int, str] = {issue_number: target_body_sha256}
    for number, body_sha in extra_nodes.items():
        nodes_map.setdefault(number, body_sha)

    nodes = [
        {"issue_number": n, "body_sha256": nodes_map[n]}
        for n in sorted(nodes_map)
    ]
    relations_sorted = sorted(
        relations,
        key=lambda r: (r["source_issue_number"], r["target_issue_number"], r["relation_type"]),
    )

    if not issues_complete or not dependencies_complete or unresolved_references:
        state = "deferred"
        defer_reason = (
            "scope rollup evidence incomplete ("
            + "; ".join(unclassified_notes or ["scope_rollup_evidence_incomplete"])
            + "); rerun issue-refinement-loop"
        )
        final_predecessors: list[int] = []
    elif duplicate_hit:
        # Reachable only if a future, genuinely reliable source (explicit
        # duplicate marker / GitHub duplicate event) populates duplicate_hit
        # -- ISSUE_SCOPE_ROLLUP_PLAN_V2 heuristics no longer set this
        # (PR #1767 owner review, P0-2).
        state = "duplicate"
        defer_reason = "duplicate or absorb relation confirmed by an authoritative source"
        final_predecessors = []
    elif predecessors:
        # Reachable only once a genuinely directional dependency source
        # (GitHub native blockedBy/blocking) populates predecessors --
        # ISSUE_SCOPE_ROLLUP_PLAN_V2 heuristics no longer do (P0-2).
        state = "blocked"
        defer_reason = (
            "open predecessor issue(s) pending: "
            + ", ".join(f"#{p}" for p in predecessors)
        )
        final_predecessors = predecessors
    elif unclassified_notes:
        # PR #1767 owner review (P0-2): ambiguous scope-rollup coordination
        # signals (undetermined direction, human_review_required, or a
        # merge/amend/absorb suggestion) must not silently resolve to
        # 'selected'. They require human classification.
        state = "deferred"
        defer_reason = (
            "unclassified scope-rollup coordination signal(s) require human "
            "classification: " + "; ".join(unclassified_notes)
        )
        final_predecessors = []
    else:
        state = "selected"
        defer_reason = None
        final_predecessors = []

    execution_block = {
        "state": state,
        "target_issue_number": issue_number,
        "predecessors": final_predecessors,
        "defer_reason": defer_reason,
    }
    completeness_block = {
        "issues_complete": issues_complete,
        "dependencies_complete": dependencies_complete,
        "unresolved_references": unresolved_references,
    }

    # PR #1767 owner review (P0-3.3): collection_digest previously covered
    # only nodes/relations, so execution.state could change (e.g. blocked ->
    # deferred) without changing the digest handoff to downstream consumers
    # claim as unchanged. Cover the full semantic decision surface instead.
    collection_digest = _sha256_prefixed(
        _canonical_json(
            {
                "nodes": nodes,
                "relations": relations_sorted,
                "execution": execution_block,
                "completeness": completeness_block,
                "downstream_policy": ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY,
            }
        )
    )

    return {
        "schema_version": ISSUE_EXECUTION_DECISION_SCHEMA_VERSION,
        "identity": {
            "target_issue_number": issue_number,
            "target_body_sha256": target_body_sha256,
            "generated_at": generated_at,
            "collection_digest": collection_digest,
        },
        "nodes": nodes,
        "relations": relations_sorted,
        "execution": execution_block,
        "downstream_policy": dict(ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY),
        "completeness": completeness_block,
    }


# validate_issue_execution_decision (schema-first combined check) and its
# validate_schema()/validate_semantics() split now live in the standalone
# canonical module validate_issue_execution_decision.py (#1677 AC11/AC12
# Scope Delta), imported above. This module no longer defines its own
# copy of the semantic invariants.


def _extract_sections(text: str) -> dict[str, str]:
    if delta_extract_sections is not None:
        return delta_extract_sections(text)
    """Extract markdown sections (e.g., ## Outcome) from text.

    AC4: fenced-code-aware — headings inside ``` or ~~~ fences are NOT treated
    as section headings. Only ## lines outside any fence are recognized.

    Per CommonMark spec, the opening fence marker is the run of backticks (or
    tildes) at the start of the line.  The closing fence must use the same
    character and have at least as many characters.  We capture the full
    opening-marker length so that a fence opened with `````markdown` (4 backticks)
    is NOT closed by a bare ` ``` ` (3 backticks) that appears inside it.
    """
    sections = {}
    current_section = None
    current_content = []
    in_fence = False
    fence_char = ""      # The backtick/tilde character used to open the fence
    fence_len = 0        # Minimum number of chars needed to close the fence

    for line in text.splitlines():
        # Detect fence open/close (``` or ~~~, optionally followed by language)
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                # Capture the full run of the fence character (could be 3+)
                fc = stripped[0]
                run_len = 0
                for ch in stripped:
                    if ch == fc:
                        run_len += 1
                    else:
                        break
                fence_char = fc
                fence_len = run_len
                in_fence = True
                if current_section is not None:
                    current_content.append(line)
                continue
        else:
            # Inside fence: look for a closing fence (same char, >= same length,
            # with no trailing non-whitespace chars other than the fence char itself).
            if stripped and all(c == fence_char for c in stripped) and len(stripped) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section is not None:
                    current_content.append(line)
                continue
            # Inside fence: pass line through without heading detection
            if current_section is not None:
                current_content.append(line)
            continue

        # Outside fence: detect headings
        if line.startswith("## "):
            if current_section is not None:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[3:].strip()
            current_content = []
        elif current_section is not None:
            current_content.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def _extract_section_lines(text: str, section_name: str) -> list[str]:
    """Extract lines from a specific section."""
    sections = _extract_sections(text)
    content = sections.get(section_name, "")
    return content.splitlines() if content else []


def _remove_fenced_code(text: str) -> str:
    """Remove fenced code blocks from text."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"~~~[\s\S]*?~~~", "", text)
    return text


def _extract_paths_from_text(text: str, exclude_fenced: bool = True) -> frozenset[str]:
    """Extract file paths from text, optionally excluding fenced code blocks."""
    if exclude_fenced:
        text = _remove_fenced_code(text)

    paths = []
    for match in PATH_PATTERN.finditer(text):
        path = match.group("path").rstrip("/").replace("\\", "/")
        # Clean up any trailing pipe or other markdown chars
        path = path.rstrip("|").strip()
        if path:
            paths.append(path)

    return frozenset(paths)


def _extract_paths_from_outcome_ac_vc(issue_body: str) -> frozenset[str]:
    """Extract target_paths from Outcome, InScope, AC, VC sections."""
    sections = _extract_sections(issue_body)
    combined_text = ""

    for section_name in ["Outcome", "In Scope", "Acceptance Criteria", "Verification Commands"]:
        combined_text += sections.get(section_name, "") + "\n"

    return _extract_paths_from_text(combined_text, exclude_fenced=True)


def _extract_repo_claims(issue_body: str) -> list[str]:
    """Extract claims about repo facts (commands, skills, schemas, paths)."""
    claims = []
    sections = _extract_sections(issue_body)

    # Include Allowed Paths section as per B6
    for section_name in ["Outcome", "In Scope", "Acceptance Criteria", "Verification Commands", "Allowed Paths"]:
        content = sections.get(section_name, "")
        # B6: Also exclude fenced code for repo_claims
        content = _remove_fenced_code(content)

        for line in content.splitlines():
            # Only extract lines that mention concrete repo elements (paths, scripts)
            # NOT generic section headers like "## Verification Commands"
            if any(
                keyword in line.lower()
                for keyword in [
                    ".claude/",
                    "src/",
                    "docs/",
                    "tests/",
                    "scripts/",
                    ".github/",
                    "$ ",  # Shell commands
                ]
            ):
                stripped = line.strip()
                if stripped and not stripped.startswith("# "):  # Exclude headers
                    claims.append(stripped)

    return claims


def _extract_critical_external_claims(
    issue_body: str, comments: Optional[list[dict[str, Any]]] = None
) -> list[dict[str, Any]]:
    """Extract critical external claims from issue body and comments."""
    claims = []
    sections = _extract_sections(issue_body)

    # Check each section for external claims in issue body
    for section_name in ["Outcome", "In Scope", "Acceptance Criteria", "Verification Commands", "Out of Scope"]:
        content = sections.get(section_name, "")
        for line in content.splitlines():
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in CRITICAL_EXTERNAL_KEYWORDS):
                claim = line.strip()
                if claim:
                    claims.append(
                        {
                            "claim": claim,
                            "affects": _infer_affects_section(section_name),
                            "source_hint": None,
                            "role": EXTERNAL_CLAIM_ROLE_DISPOSITIVE,
                        }
                    )

    # B4: Check comments for human-requested web verification
    if comments and isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict):
                comment_body = comment.get("body", "")
                comment_id = comment.get("id", comment.get("comment_id"))

                for keyword in HUMAN_WEB_VERIFICATION_KEYWORDS:
                    if keyword in comment_body.lower():
                        # Found human request for web verification in comments
                        claim_text = (
                            comment_body[:100].strip()
                            if comment_body
                            else "Human requested web verification"
                        )
                        claims.append(
                            {
                                "claim": claim_text,
                                "affects": "VC",
                                "source_hint": (
                                    f"comment_{comment_id}" if comment_id else "comment"
                                ),
                                "role": EXTERNAL_CLAIM_ROLE_DISPOSITIVE,
                            }
                        )
                        break  # Only add once per comment

    # B7: Stable sort and dedupe by claim text
    claims_by_text = {}
    for claim in claims:
        key = claim["claim"]
        if key not in claims_by_text:
            claims_by_text[key] = claim

    # Return sorted deduped claims
    return sorted(claims_by_text.values(), key=lambda c: c["claim"])


def _infer_affects_section(section_name: str) -> str:
    """Map section name to 'affects' enum value."""
    mapping = {
        "Outcome": "Outcome",
        "In Scope": "InScope",
        "Acceptance Criteria": "AC",
        "Verification Commands": "VC",
        "Out of Scope": "StopCondition",
    }
    return mapping.get(section_name, "AC")


def _extract_unmaterialized_slots(issue_body: str) -> list[dict[str, Any]]:
    """Extract unmaterialized child slots from issue body."""
    slots = []
    lines = issue_body.splitlines()

    for line_no, line in enumerate(lines, start=1):
        if UNMATERIALIZED_MARKER_PATTERN.search(line):
            # Extract title hint from line
            title_hint = re.sub(
                r"(?:（未起票）|（未起票）|\(未起票\)|unmaterialized|TBD)",
                "",
                line,
            ).strip()

            # Determine marker type
            if "未起票" in line:
                marker = "未起票"
            elif "unmaterialized" in line:
                marker = "unmaterialized"
            else:
                marker = "TBD"

            slots.append(
                {
                    "child_title_hint": title_hint,
                    "marker": marker,
                    "body_line": line_no,
                }
            )

    return slots


def _extract_follow_up_candidates(issue_body: str) -> list[dict[str, Any]]:
    """Extract follow-up issue candidates from Out of Scope or Stop Conditions."""
    candidates = []
    sections = _extract_sections(issue_body)

    for section_name in ["Out of Scope", "Stop Conditions"]:
        content = sections.get(section_name, "")
        lines = content.splitlines()

        for line_no, line in enumerate(lines, start=1):
            if "follow-up" in line.lower() or "別 issue" in line.lower():
                summary = line.strip()
                if summary:
                    # Create dedupe_key as first 16 chars of sha256(summary)
                    dedupe_key = _sha256(summary)[:16]
                    candidates.append(
                        {
                            "dedupe_key": dedupe_key,
                            "summary": summary,
                            "source_evidence": {
                                "source": "issue_body",
                                "source_ref": None,
                                "start_line": line_no,
                                "end_line": line_no,
                                "text_sha256": _sha256(summary),
                            },
                        }
                    )

    # Dedupe by dedupe_key
    seen_keys = set()
    deduped = []
    for candidate in candidates:
        key = candidate["dedupe_key"]
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(candidate)

    return deduped


def _is_anchor_reframe_context(known_context: dict | None) -> bool:
    """Check if known_context indicates anchor reframe exclusion."""
    if not known_context:
        return False
    has_scope_signal_delta_input = "scope_signal_delta_input" in known_context
    scope_delta_decision = known_context.get("scope_delta_decision")
    if isinstance(scope_delta_decision, dict):
        if (
            scope_delta_decision.get("status") == "approved_by_trusted_anchor"
            and scope_delta_decision.get("implementation_go") is False
            and scope_delta_decision.get("required_rerun")
        ):
            return True
    if has_scope_signal_delta_input:
        return False
    classification = known_context.get("classification")
    if classification in ("feedback_update_required", "reframe_in_place"):
        return True
    if known_context.get("anchor_comment_url") and known_context.get("anchor_reframe", False):
        return True
    return False


def _delta_projection_to_evidence_spans(delta_result: dict[str, Any]) -> list[dict[str, Any]]:
    projection = delta_result.get("legacy_scope_signal_guard", {})
    source_refs = (delta_result.get("inputs") or {}).get("source_refs") or {}
    evidence_spans = []
    for line in projection.get("triggering_lines", []):
        body_version = line["body_version"]
        source_ref = source_refs.get(body_version)
        evidence_spans.append(
            {
                "source": "known_context",
                "source_ref": source_ref,
                "start_line": line["start_line"],
                "end_line": line["end_line"],
                "body_version": body_version,
                "coordinate_space": "body_absolute_1_based",
                "text_sha256": line["text_sha256"].removeprefix("sha256:"),
            }
        )
    return evidence_spans


def _compute_scope_signal_delta_result(known_context: dict) -> dict[str, Any]:
    """Compute the scope_signal_delta result once, fail-closed on any error.

    Shared by the legacy scope_signal_guard projection and the
    SCOPE_SIGNAL_GUARD_DECISION_V2 build (#1090) so both consume the SAME
    delta result; a delta failure raises ScopeSignalDeltaError (fail-closed
    plan) instead of being silently swallowed by either consumer.
    """
    if compute_scope_signal_delta is None:
        raise ScopeSignalDeltaError("scope_signal_delta helper is unavailable")
    if not isinstance(known_context.get("scope_signal_delta_input"), dict):
        raise ScopeSignalDeltaError("scope_signal_delta_input must be an object")
    try:
        return compute_scope_signal_delta(known_context["scope_signal_delta_input"])
    except Exception as exc:
        raise ScopeSignalDeltaError(f"scope_signal_delta_input is invalid: {exc}") from exc


_SCOPE_SIGNAL_LINE_PATH_TOKEN_RE = re.compile(
    r"`([^`\n]+)`|((?:\.claude|docs|src|scripts|tests|\.github)/[^\s`]+)"
)


def _line_layer_prefixes(line: str, prefixes) -> set:
    """Extract the set of known layer prefixes that a line's path tokens start with.

    A single path token (backtick-quoted or bare) may contain more than one of
    the known prefixes as an embedded substring (e.g. a test file nested under
    a skill directory such as ".claude/skills/<skill>/tests/<file>.py" contains
    both ".claude/" and "tests/"). Counting that as two independent layer
    mentions is a false positive (Issue #1327). This helper instead extracts
    whole path-like tokens and only attributes a prefix to a token when the
    token itself *starts with* that prefix, so a prefix appearing mid-token
    never counts as an additional layer.
    """
    found = set()
    for match in _SCOPE_SIGNAL_LINE_PATH_TOKEN_RE.finditer(line):
        candidate = (match.group(1) or match.group(2) or "").strip()
        for prefix in prefixes:
            if candidate.startswith(prefix):
                found.add(prefix)
                break
    return found


def _iter_section_source_lines(text: str, section_name: str) -> list[SourceLine]:
    if delta_iter_section_lines is not None:
        return delta_iter_section_lines(text, section_name, semantic_only=False)
    lines: list[SourceLine] = []
    current_section: str | None = None
    in_fence = False
    fence_char = ""
    fence_len = 0

    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_char = stripped[0]
                fence_len = len(stripped) - len(stripped.lstrip(fence_char))
                in_fence = True
                if current_section == section_name:
                    lines.append(SourceLine(index, line))
                continue
        else:
            if stripped and all(ch == fence_char for ch in stripped) and len(stripped) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section == section_name:
                    lines.append(SourceLine(index, line))
                continue
            if current_section == section_name:
                lines.append(SourceLine(index, line))
            continue

        if line.startswith("## "):
            current_section = line[3:].strip()
            continue
        if current_section == section_name:
            lines.append(SourceLine(index, line))

    return lines


def _iter_section_semantic_lines(text: str, section_name: str) -> list[SourceLine]:
    if delta_iter_section_lines is not None:
        return delta_iter_section_lines(text, section_name, semantic_only=True)
    return _iter_section_source_lines(text, section_name)


def _valid_scope_signal_source_ref(value: Any, *, issue_number: int | None) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if issue_number is None:
        return True
    if not value.startswith("artifact:"):
        return False
    return f"/issue-refinement-loop/{issue_number}/snapshots/" in value


def _has_valid_scope_signal_delta_input(
    issue_number: int | None,
    known_context: dict | None,
) -> bool:
    if not isinstance(known_context, dict):
        return False
    payload = known_context.get("scope_signal_delta_input")
    if not isinstance(payload, dict):
        return False
    for field in ("before_body", "current_body", "after_body"):
        if not isinstance(payload.get(field), str):
            return False
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, dict):
        return False
    if not _valid_scope_signal_source_ref(source_refs.get("before"), issue_number=issue_number):
        return False
    if not _valid_scope_signal_source_ref(source_refs.get("current"), issue_number=issue_number):
        return False
    after_ref = source_refs.get("after")
    if after_ref is not None and not _valid_scope_signal_source_ref(after_ref, issue_number=issue_number):
        return False
    if (
        payload["before_body"] == payload["current_body"]
        and source_refs.get("before") == source_refs.get("current")
    ):
        return False
    return True


def _scope_signal_delta_required(issue_body: str, known_context: dict | None) -> bool:
    if isinstance(known_context, dict) and "scope_signal_delta_required" in known_context:
        return bool(known_context["scope_signal_delta_required"])
    lowered = issue_body.lower()
    return any(
        token in lowered
        for token in ("scope_signal_guard", "scope_signal_delta_input", "new_allowed_path_layer")
    )


def _requires_delta_fail_closed(
    issue_number: int | None,
    issue_body: str,
    known_context: dict | None,
) -> bool:
    if not known_context:
        return False
    if not _scope_signal_delta_required(issue_body, known_context):
        return False
    current_phase = known_context.get("current_phase")
    if current_phase not in _HARD_STOP_SCOPE_SIGNAL_PHASES:
        return False
    return not _has_valid_scope_signal_delta_input(issue_number, known_context)


def _detect_scope_signals(
    issue_number_or_body: int | str,
    issue_body_or_known_context: str | dict | None,
    known_context: dict | None = None,
    precomputed_delta: "dict[str, Any] | None" = None,
) -> tuple[bool, str, list]:
    """
    Detect scope signals (new_in_scope_area, new_allowed_path_layer, new_unverifiable_ac).

    Returns (triggered, reason_code, evidence_spans)

    Precedence: new_unverifiable_ac > new_allowed_path_layer > new_in_scope_area > none
    """
    if isinstance(issue_number_or_body, int):
        issue_number = issue_number_or_body
        issue_body = str(issue_body_or_known_context)
    else:
        issue_number = None
        issue_body = issue_number_or_body
        known_context = issue_body_or_known_context if isinstance(issue_body_or_known_context, dict) else None

    if _requires_delta_fail_closed(issue_number, issue_body, known_context):
        raise ScopeSignalDeltaError(
            "scope_signal_delta_input is required for new_allowed_path_layer in hard-stop phases",
            reason_code=SCOPE_SIGNAL_REASON_MISSING_DELTA_INPUT,
        )

    if known_context and "scope_signal_delta_input" in known_context:
        delta_result = (
            precomputed_delta
            if precomputed_delta is not None
            else _compute_scope_signal_delta_result(known_context)
        )
        projection = delta_result.get("legacy_scope_signal_guard", {})
        evidence_spans = _delta_projection_to_evidence_spans(delta_result)
        if projection.get("triggered"):
            if _is_anchor_reframe_context(known_context):
                return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
            return (
                True,
                projection.get("reason_code", SCOPE_SIGNAL_REASON_NO_SIGNAL),
                evidence_spans,
            )
        return (False, SCOPE_SIGNAL_REASON_NO_SIGNAL, [])

    evidence_spans = []
    acceptance_criteria_lines = _iter_section_semantic_lines(issue_body, "Acceptance Criteria")
    allowed_path_lines = _iter_section_semantic_lines(issue_body, "Allowed Paths")
    in_scope_lines = _iter_section_semantic_lines(issue_body, "In Scope")

    # Subjective keywords indicating unverifiable AC
    subjective_keywords = [
        "適切に", "品質を改善", "安定する", "改善する", "最適化", "高品質に",
        "improve", "enhance", "optimize", "stabilize", "appropriately"
    ]

    # 1. Check new_unverifiable_ac (highest precedence)
    for source_line in acceptance_criteria_lines:
        line = source_line.text
        if line.lstrip().startswith("- [ ]") or line.lstrip().startswith("- [x]"):
            if any(kw in line for kw in subjective_keywords):
                evidence_spans.append({
                    "source": "issue_body",
                    "source_ref": None,
                    "start_line": source_line.number,
                    "end_line": source_line.number,
                    "text_sha256": _sha256(line),
                })
                # Check exclusion
                excluded = _is_anchor_reframe_context(known_context)
                if excluded:
                    return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
                return (True, SCOPE_SIGNAL_REASON_NEW_UNVERIFIABLE_AC, evidence_spans)

    # 2. Check new_allowed_path_layer
    top_level_prefixes = set()
    for source_line in allowed_path_lines:
        match = re.search(r'`([^/`]+)/', source_line.text)
        if match:
            top_level_prefixes.add(match.group(1))

    if len(top_level_prefixes) >= 2:
        # Find evidence line
        for source_line in allowed_path_lines:
            line = source_line.text
            if any(p in line for p in top_level_prefixes):
                evidence_spans.append({
                    "source": "issue_body",
                    "source_ref": None,
                    "start_line": source_line.number,
                    "end_line": source_line.number,
                    "text_sha256": _sha256(line),
                })
                break
        excluded = _is_anchor_reframe_context(known_context)
        if excluded:
            return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
        return (True, SCOPE_SIGNAL_REASON_NEW_PATH_LAYER, evidence_spans)

    # 3. Check new_in_scope_area
    #
    # Note: a single path token (e.g. a backtick-quoted or bare path) may
    # contain more than one of the known prefixes as an embedded substring
    # (for example ".claude/skills/<skill>/tests/<file>.py" contains both
    # ".claude/" and "tests/"). Counting that as two independent layer
    # mentions is a false positive (Issue #1327). We instead extract whole
    # path-like tokens per line and only attribute a prefix to a token when
    # the token itself *starts with* that prefix, so nested substrings
    # within a single token never count as extra layers.
    layer_prefixes_in_scope = set()
    for source_line in in_scope_lines:
        layer_prefixes_in_scope |= _line_layer_prefixes(
            source_line.text, (".claude/", "docs/", "src/", "scripts/", "tests/", ".github/")
        )

    if len(layer_prefixes_in_scope) >= 2:
        # Find evidence line
        for source_line in in_scope_lines:
            line = source_line.text
            if _line_layer_prefixes(line, tuple(layer_prefixes_in_scope)):
                evidence_spans.append({
                    "source": "issue_body",
                    "source_ref": None,
                    "start_line": source_line.number,
                    "end_line": source_line.number,
                    "text_sha256": _sha256(line),
                })
                break
        excluded = _is_anchor_reframe_context(known_context)
        if excluded:
            return (False, SCOPE_SIGNAL_REASON_ANCHOR_REFRAME, evidence_spans)
        return (True, SCOPE_SIGNAL_REASON_NEW_IN_SCOPE, evidence_spans)

    return (False, SCOPE_SIGNAL_REASON_NO_SIGNAL, [])


def _classify_path_layer(path: str) -> str:
    """AC1: classify an Allowed Path entry into a coarse scope_context.path_layer.

    Precedence matters: more specific prefixes (.claude/hooks, .claude/agents,
    .claude/skills) are checked before the generic tests/fixtures bucket.
    """
    normalized = (path or "").strip().replace("\\", "/").rstrip("/")
    if normalized.startswith(".claude/hooks/") or normalized == ".claude/hooks":
        return PATH_LAYER_HOOK
    if normalized.startswith(".claude/agents/") or normalized == ".claude/agents":
        return PATH_LAYER_AGENT
    if normalized.startswith(".claude/skills/") or normalized == ".claude/skills":
        return PATH_LAYER_SKILL
    if normalized.startswith("docs/"):
        return PATH_LAYER_DOCS
    if normalized.startswith("src/"):
        return PATH_LAYER_RUNTIME
    if (
        normalized.startswith("tests/")
        or normalized.startswith("fixtures/")
        or "/tests/" in normalized
        or "/fixtures/" in normalized
    ):
        return PATH_LAYER_TEST_FIXTURE
    return PATH_LAYER_UNKNOWN


def _is_security_sensitive_scope_delta(added_paths: list[str], rationale_text: "str | None") -> bool:
    """AC13: deterministic security-sensitive gate, not overridable by approval."""
    for path in added_paths or []:
        normalized = (path or "").strip()
        for prefix in SECURITY_SENSITIVE_PATH_PREFIXES:
            if normalized.startswith(prefix):
                return True
        if _SECURITY_SENSITIVE_TERM_RE.search(normalized.lower()):
            return True
    if rationale_text:
        if _SECURITY_SENSITIVE_TERM_RE.search(rationale_text.lower()):
            return True
    return False


def _extract_scope_delta_approval_evidence(known_context: "dict | None") -> "dict | None":
    """AC10: read normalized (non-raw) approval evidence from known_context.

    The evidence dict is produced upstream (orchestrator/checker) from the
    ANCHOR_SCOPE_REFRAME comment; this function never parses raw comment body.
    """
    if not known_context:
        return None
    evidence = known_context.get("scope_delta_approval_evidence")
    if not isinstance(evidence, dict):
        return None
    return evidence


def _base_approval_result() -> dict:
    """Missing-approval baseline for the scope_delta_approval artifact field."""
    return {
        "present": False,
        "valid": False,
        "status": "missing",
        "missing_approval_field": True,
        "suggested_contract_patch": SUGGESTED_CONTRACT_PATCH_TEMPLATE,
        "comment_id": None,
        "comment_url": None,
        "body_sha256": None,
        "author_association": None,
        "created_at": None,
        "issue_url": None,
        "required_rerun": [],
    }


def _expected_repo_for_issue(issue: "dict | None", known_context: "dict | None") -> "str | None":
    """Derive the expected `owner/name` repo for AC8 structural URL validation.

    Sources (in order): known_context.repo, issue.html_url / issue.url
    (GitHub issue URL forms). Returns None when the repo cannot be
    established — evidence-based approval then fails closed.
    """
    if known_context:
        repo = known_context.get("repo")
        if isinstance(repo, str) and repo.count("/") == 1 and all(repo.split("/")):
            return repo
    for key in ("html_url", "url"):
        candidate = (issue or {}).get(key)
        if not isinstance(candidate, str) or not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme != "https":
            continue
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc == "github.com" and len(parts) >= 4 and parts[2] == "issues":
            return f"{parts[0]}/{parts[1]}"
        if parsed.netloc == "api.github.com" and len(parts) >= 5 and parts[0] == "repos" and parts[3] == "issues":
            return f"{parts[1]}/{parts[2]}"
    return None


def _parse_github_issue_comment_url(url: "str | None") -> "dict | None":
    """Parse a GitHub issue comment URL structurally (no substring checks).

    Accepts only https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>.
    PR review discussion URLs (#discussion_r...) and /pull/ paths are rejected
    (returns None), as are non-GitHub hosts and malformed fragments.
    """
    if not isinstance(url, str) or not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 4 or parts[2] != "issues":
        return None
    if not parts[3].isdigit():
        return None
    fragment_match = re.fullmatch(r"issuecomment-(\d+)", parsed.fragment or "")
    if fragment_match is None:
        return None
    return {
        "owner": parts[0],
        "repo": parts[1],
        "issue_number": int(parts[3]),
        "comment_id": fragment_match.group(1),
    }


def _issue_url_matches(issue_url: "str | None", expected_repo: str, issue_number: int) -> bool:
    """AC8: structural check that evidence.issue_url points at the target issue."""
    if not isinstance(issue_url, str) or not issue_url:
        return False
    expected_api = f"https://api.github.com/repos/{expected_repo}/issues/{issue_number}"
    expected_html = f"https://github.com/{expected_repo}/issues/{issue_number}"
    return issue_url.lower() in (expected_api.lower(), expected_html.lower())


def _validate_scope_delta_approval(
    evidence: "dict | None",
    current_issue_number: "int | None",
    expected_repo: "str | None" = None,
) -> dict:
    """AC2/AC8/AC9: validate scope delta approval evidence, fail-closed.

    Returns a dict with: present, valid, status, missing_approval_field,
    suggested_contract_patch, comment_id, comment_url, body_sha256,
    author_association, created_at, issue_url, required_rerun.

    AC8 is enforced structurally: comment_url must be the target issue's own
    GitHub issue comment URL (owner/repo/issue_number/comment_id all
    verified), issue_url must point at the same issue, and PR review
    discussion URLs / external hosts are rejected. When expected_repo cannot
    be established the approval fails closed as invalid.
    """
    base = _base_approval_result()
    if evidence is None:
        return base

    base["present"] = True
    base["comment_id"] = evidence.get("comment_id")
    base["comment_url"] = evidence.get("comment_url")
    base["body_sha256"] = evidence.get("body_sha256")
    base["author_association"] = evidence.get("author_association")
    base["created_at"] = evidence.get("created_at")
    base["issue_url"] = evidence.get("issue_url")
    required_rerun = evidence.get("required_rerun")
    base["required_rerun"] = required_rerun if isinstance(required_rerun, list) else []

    marker_present = bool(evidence.get("marker_present"))
    if not marker_present:
        base["status"] = "missing_marker"
        base["missing_approval_field"] = True
        return base

    def _invalid() -> dict:
        base["status"] = "invalid_scope_delta_approval"
        base["missing_approval_field"] = False
        base["suggested_contract_patch"] = None
        return base

    # AC8: approval only valid for the current issue's own comment URL.
    target_issue_number = evidence.get("target_issue_number")
    if current_issue_number is not None and target_issue_number != current_issue_number:
        return _invalid()

    # AC8: structural comment_url validation (host / owner / repo / issue /
    # comment id), fail-closed when the runtime repo cannot be established.
    parsed_comment = _parse_github_issue_comment_url(evidence.get("comment_url"))
    if parsed_comment is None:
        return _invalid()
    if expected_repo is None:
        return _invalid()
    expected_parts = expected_repo.lower().split("/", 1)
    if [parsed_comment["owner"].lower(), parsed_comment["repo"].lower()] != expected_parts:
        return _invalid()
    if current_issue_number is not None and parsed_comment["issue_number"] != current_issue_number:
        return _invalid()
    evidence_comment_id = evidence.get("comment_id")
    if evidence_comment_id is None or str(evidence_comment_id) != parsed_comment["comment_id"]:
        return _invalid()
    if current_issue_number is not None and not _issue_url_matches(
        evidence.get("issue_url"), expected_repo, current_issue_number
    ):
        return _invalid()

    # AC9: only OWNER/MEMBER/COLLABORATOR are trusted authors.
    author_association = evidence.get("author_association")
    if author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
        return _invalid()

    base["valid"] = True
    base["status"] = "approved"
    base["missing_approval_field"] = False
    base["suggested_contract_patch"] = None
    return base


def _compute_scope_reframe_rewrite_route(known_context: "dict | None") -> "dict | None":
    """#2048: echo the rewrite-loop scope-reframe/empty-operations router
    decision alongside `decisions.scope_delta_decision`.

    This is opt-in and purely additive: it only runs when
    `known_context["scope_delta_decision"]` is present AND carries an
    `operations` key (a CONTRACT_PATCH_PLAN_V1 operations[] list the caller
    derived for this approved trusted-anchor scope reframe). Callers that
    never set the `scope_delta_decision` key, or set it without an
    `operations` key at all, (every pre-#2048 fixture) get `None` back and
    the planner's `scope_delta_decision` echo is unchanged.

    PR #2057 OWNER REQUEST_CHANGES revision (P0-1 planner/consumer parity):
    this now calls the SAME single canonical
    `decide_rewrite_route.classify_scope_reframe_disposition()` pure
    function that `run_refinement_preflight.consume_trusted_anchor_
    contract_patch_plan()` calls, on the same raw inputs -- so the two
    layers cannot disagree on `patch` / `proven_no_change` /
    `full_rewrite_required` / `invalid`. Previously this coerced a
    non-list `operations` to `[]` (treating a malformed/untyped payload as
    an ordinary empty-operations no-op) while the consumer boundary failed
    closed on the exact same input -- the two layers could reach opposite
    verdicts for identical malformed input. Non-list `operations` is now
    `invalid` here too.

    `known_context["scope_reframe_deltas_reflected_status"]` (optional;
    one of `"present"`/`"absent"`/`"invalid_or_unavailable"`) lets a caller
    that has independently computed the Allowed-Paths-reflected tri-state
    thread it through for a `proven_no_change` echo; it defaults to
    `"absent"` (this read-only planner never re-reads the live Issue body
    itself), preserving this function's pre-existing behavior for every
    caller that does not supply it.

    Also folds in the AC4 rewrite-router `implementation_allowed` gate under
    the collision-avoiding key `rewrite_router_implementation_allowed` when
    the caller supplies `known_context["rewrite_readback_status"]` (body/title
    readback + fresh review/preflight confirmation flags) -- see the
    naming-collision note at the top of this module.
    """
    if not isinstance(known_context, dict):
        return None
    decision = known_context.get("scope_delta_decision")
    if not isinstance(decision, dict):
        return None
    if "operations" not in decision:
        # Caller did not supply a derived operations[] key at all -- nothing
        # to route (distinct from an explicit `operations: []`, which IS
        # classified below).
        return None

    if _SCOPE_REFRAME_ROUTER_IMPORT_FAILED or classify_scope_reframe_disposition is None:
        return {
            "schema_version": "scope_reframe_decision/v1",
            "disposition": "invalid",
            "reason_code": "scope_reframe_router_import_failed",
            "route": None,
        }

    reflected_status = known_context.get("scope_reframe_deltas_reflected_status", "absent")
    decision_result = classify_scope_reframe_disposition(
        scope_delta_status=decision.get("status"),
        operations_key_present=True,
        operations=decision.get("operations"),
        allowed_path_deltas_raw=decision.get("allowed_path_deltas"),
        reflected_status=reflected_status,
        anchor_comment_url=decision.get("anchor_comment_url"),
        issue_body_sha256=known_context.get("issue_body_sha256"),
        previous_empty_operations_fingerprints=known_context.get("previous_empty_operations_fingerprints"),
    )
    result = decision_result.to_dict()

    readback = known_context.get("rewrite_readback_status")
    if isinstance(readback, dict) and _rewrite_router_compute_implementation_allowed is not None:
        result["rewrite_router_implementation_allowed"] = (
            _rewrite_router_compute_implementation_allowed(
                route=result.get("route"),
                body_readback_confirmed=bool(readback.get("body_readback_confirmed")),
                title_readback_confirmed=bool(readback.get("title_readback_confirmed")),
                fresh_review_preflight_completed=bool(
                    readback.get("fresh_review_preflight_completed")
                ),
            )
        )

    return result


def _project_scope_delta_decision_to_approval(known_context: "dict | None") -> dict:
    """Blocker 1 (#1294 review): adapter from the production anchor approval path.

    `run_refinement_preflight.py` validates the anchor comment URL
    structurally (`_validate_anchor_comment_url`) and classifies the
    ANCHOR_SCOPE_REFRAME_V1 payload into `known_context.scope_delta_decision`
    (#920/#1027 contract). This adapter projects that upstream-verified
    decision into the v2 `scope_delta_approval` shape so trusted anchor
    approvals reach `proceed_with_notes` without a parallel
    `scope_delta_approval_evidence` producer.
    """
    base = _base_approval_result()
    decision = (known_context or {}).get("scope_delta_decision")
    if not isinstance(decision, dict):
        return base

    # #2156 AC7: `status: not_applicable` is no longer treated as "no
    # anchor reframe was attempted at all" (bare `missing` lane, evidence
    # fields left at their `_base_approval_result()` defaults). Since #2156
    # AC2, `_classify_anchor_scope_reframe()` returns `status:
    # not_applicable` for the genuine-absence case where a trusted-author
    # anchor comment DOES exist but simply carries no ANCHOR_SCOPE_REFRAME_V1
    # marker -- the same scenario the `reason ==
    # "no_anchor_scope_reframe_v1_payload"` branch below already projects to
    # `status: missing_marker`, with the trusted author's anchor comment
    # evidence (comment_url / body_sha256 / author_association /
    # required_rerun) populated below rather than dropped. Falling through
    # here (instead of returning `base` early) is what preserves that
    # evidence for the genuine-absence case.
    #
    # PR #2171 fix_delta (P1-4, OWNER adversarial review): the above is
    # deliberately scoped to `status == not_applicable` combined with
    # `reason == no_anchor_scope_reframe_v1_payload` ONLY. A different
    # `not_applicable` producer -- a bare `{"status": "not_applicable"}`
    # with no reason, or an unrelated reason string -- carries no
    # trustworthy anchor-comment evidence and must not fall through to the
    # `invalid_scope_delta_approval` branch below (which would
    # mischaracterize "no reframe info available" as "a reframe was
    # attempted but rejected").
    if decision.get("status") == "not_applicable" and decision.get("reason") != (
        "no_anchor_scope_reframe_v1_payload"
    ):
        return base

    base["present"] = True
    base["comment_url"] = decision.get("anchor_comment_url")
    base["body_sha256"] = decision.get("anchor_comment_hash")
    base["author_association"] = decision.get("anchor_author_association")
    required_rerun = decision.get("required_rerun")
    base["required_rerun"] = required_rerun if isinstance(required_rerun, list) else []

    if (
        decision.get("status") == "approved_by_trusted_anchor"
        and decision.get("implementation_go") is False
        and decision.get("anchor_author_association") in TRUSTED_AUTHOR_ASSOCIATIONS
    ):
        base["valid"] = True
        base["status"] = "approved"
        base["missing_approval_field"] = False
        base["suggested_contract_patch"] = None
        return base

    if decision.get("reason") == "no_anchor_scope_reframe_v1_payload":
        # A comment exists but carries no reframe marker: same lane as
        # missing_marker (AC3), keep the suggested contract patch.
        base["status"] = "missing_marker"
        return base

    # Reframe attempted but rejected upstream (untrusted author, wrong
    # repo/issue, schema invalid): AC8/AC9 invalid lane.
    base["status"] = "invalid_scope_delta_approval"
    base["missing_approval_field"] = False
    base["suggested_contract_patch"] = None
    return base


def _decide_scope_signal_route(
    triggered: bool,
    security_sensitive: bool,
    approval: dict,
) -> str:
    """AC2/AC3/AC4/AC8/AC9/AC13: decide the escalation lane.

    status="missing" (no evidence at all) and status="missing_marker"
    (evidence present but no ANCHOR_SCOPE_REFRAME/Scope Delta Approval/
    Allowed Paths Expansion Rationale marker) both mean "no reframe" (AC3)
    -> human_judgment_required. status="invalid_scope_delta_approval"
    means a reframe was attempted but failed the target-issue (AC8) or
    trusted-author (AC9) check -> invalid_scope_delta_approval.
    """
    if security_sensitive:
        # AC13: security-sensitive fail-closed gate is never overridden by
        # approval, and takes precedence over not_triggered so that a
        # sensitive path added within an already-allowed layer (no
        # new_allowed_path_layer signal) still gates.
        return SCOPE_ROUTE_SECURITY_RISK_GATE_REQUIRED
    if not triggered:
        return SCOPE_ROUTE_NOT_TRIGGERED
    if approval["status"] in ("missing", "missing_marker"):
        # AC3: no reframe present at all.
        return SCOPE_ROUTE_HUMAN_JUDGMENT_REQUIRED
    if approval["status"] == "invalid_scope_delta_approval":
        # AC8/AC9: reframe present but invalid (wrong issue / untrusted author).
        return SCOPE_ROUTE_INVALID_SCOPE_DELTA_APPROVAL
    # AC2/AC4/AC12: valid trusted anchor approval -- proceed with contract-review rerun required.
    return SCOPE_ROUTE_PROCEED_WITH_NOTES


def _build_scope_signal_guard_decision_v2(
    scope_signal_triggered: bool,
    scope_signal_reason: str,
    added_paths: list[str],
    known_context: "dict | None",
    issue_number: "int | None",
    issue: "dict | None" = None,
) -> dict:
    """AC1/AC10: build SCOPE_SIGNAL_GUARD_DECISION_V2 artifact.

    `scope_signal_triggered` / `scope_signal_reason` are the RAW
    (pre-anchor-reframe-exclusion) signal from the shared delta projection,
    so a trusted anchor approval routes to proceed_with_notes instead of
    collapsing to not_triggered (Blocker 1, PR #1294 review).
    """
    path_layers = sorted({_classify_path_layer(p) for p in (added_paths or [])})
    evidence = _extract_scope_delta_approval_evidence(known_context)
    if evidence is not None:
        expected_repo = _expected_repo_for_issue(issue, known_context)
        approval = _validate_scope_delta_approval(evidence, issue_number, expected_repo)
    else:
        approval = _project_scope_delta_decision_to_approval(known_context)
    rationale_text = evidence.get("rationale") if evidence else None
    security_sensitive = _is_security_sensitive_scope_delta(added_paths, rationale_text)
    route = _decide_scope_signal_route(scope_signal_triggered, security_sensitive, approval)

    if route == SCOPE_ROUTE_NOT_TRIGGERED:
        # No scope signal: never emit "approval missing" diagnostics that
        # could mislead downstream consumers reading only these fields.
        approval = {
            **approval,
            "status": "not_required",
            "missing_approval_field": False,
            "suggested_contract_patch": None,
        }

    return {
        "schema_version": "SCOPE_SIGNAL_GUARD_DECISION_V2",
        "raw_signal": {
            "triggered": scope_signal_triggered,
            "reason_code": scope_signal_reason,
        },
        "scope_context": {
            "path_layer": path_layers,
        },
        "scope_delta_approval": {
            "present": approval["present"],
            "valid": approval["valid"],
            "status": approval["status"],
            "missing_approval_field": approval["missing_approval_field"],
            "suggested_contract_patch": approval["suggested_contract_patch"],
            "comment_id": approval["comment_id"],
            "comment_url": approval["comment_url"],
            "body_sha256": approval["body_sha256"],
            "author_association": approval["author_association"],
            "created_at": approval["created_at"],
            "issue_url": approval["issue_url"],
            "required_rerun": approval["required_rerun"],
        },
        "security_sensitive": security_sensitive,
        "route": route,
    }


def _check_malformed_contract(issue_body: str) -> bool:
    """Check if machine-readable contract is malformed (has YAML but missing required fields)."""
    # If there's no YAML block, it's not malformed - just missing
    if "```yaml" not in issue_body:
        return False

    # If there's a YAML block, check it has required fields
    yaml_match = re.search(r"```yaml\n(.*?)\n```", issue_body, re.DOTALL)
    if not yaml_match:
        return False

    yaml_content = yaml_match.group(1)
    # If YAML block exists but is missing contract_schema_version, it's malformed
    if "contract_schema_version:" not in yaml_content:
        return True

    return False


def _check_missing_outcome(issue_body: str) -> bool:
    """Check if Outcome section is missing (fence-aware heading detection)."""
    sections = _extract_sections(issue_body)
    return "Outcome" not in sections


def _check_missing_ac(issue_body: str) -> bool:
    """Check if Acceptance Criteria section is missing (fence-aware heading detection)."""
    sections = _extract_sections(issue_body)
    return "Acceptance Criteria" not in sections


def _check_missing_vc(issue_body: str) -> bool:
    """Check if Verification Commands section is missing (fence-aware heading detection)."""
    sections = _extract_sections(issue_body)
    return "Verification Commands" not in sections


# Heading pattern for extracting headings (AC4: fenced-code-aware)
HEADING_RE = re.compile(r"^[ \t]{0,3}##[ \t]+(?P<title>.+?)[ \t#]*$", re.MULTILINE)


def _extract_machine_contract(issue_body: str) -> dict | None:
    """
    Extract Machine-Readable Contract from issue body.

    Finds the fenced YAML block that is the value of the
    'Machine-Readable Contract' section and parses it with yaml.safe_load.

    Returns None if:
    - No Machine-Readable Contract section found
    - No YAML fenced block found in that section
    - yaml.safe_load fails
    - Result is not a dict (AC3: must be dict)
    - yaml module is unavailable

    AC4: Only parses the YAML in the Machine-Readable Contract section,
    not any arbitrary fenced code block.
    """
    if not _YAML_AVAILABLE:
        return None

    sections = _extract_sections(issue_body)
    contract_section = sections.get("Machine-Readable Contract", "")
    if not contract_section:
        return None

    # Extract the first fenced yaml block from the contract section
    yaml_match = re.search(r"```yaml\n([\s\S]*?)\n```", contract_section)
    if not yaml_match:
        return None

    yaml_content = yaml_match.group(1)
    try:
        parsed = _yaml_module.safe_load(yaml_content)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def resolve_issue_template(issue_kind: str, repo_root: Path) -> Path | None:
    """
    Resolve the issue template file for the given issue_kind.

    Blocker 3: issue_kind is validated against ISSUE_KIND_ALLOWLIST before
    being used in path construction. Unknown values return None (caller must
    handle as unknown_issue_kind).

    Returns the Path to .github/ISSUE_TEMPLATE/<issue_kind>.yml,
    or None if the file does not exist or issue_kind is not in the allowlist.
    """
    try:
        allowlist = _get_issue_kind_allowlist()
    except IssueKindPolicyLoadError:
        return None
    if issue_kind not in allowlist:
        return None

    template_dir = (repo_root / ".github" / "ISSUE_TEMPLATE").resolve()
    template_path = (template_dir / f"{issue_kind}.yml").resolve()

    # Path traversal guard: resolved template must be inside template_dir
    try:
        template_path.relative_to(template_dir)
    except ValueError:
        return None

    if template_path.exists():
        return template_path
    return None


def load_required_section_labels(template_path: Path) -> TemplateLoadResult:
    """
    Load required section labels from an issue template YAML file.

    Returns TemplateLoadResult with:
    - required_labels: list of label strings where validations.required == True
    - error: None on success; FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE on failure

    Blocker 2: failure modes are distinguished from "zero required sections".
    - yaml unavailable → error=template_required_sections_unavailable
    - file missing / unreadable → error=template_required_sections_unavailable
    - parse error → error=template_required_sections_unavailable
    - body not a list → error=template_required_sections_unavailable
    - template exists with 0 required items → error=None, required_labels=[]

    AC5: Must not hardcode section names — derive from template.
    AC6: When template changes, detection changes without script modification.
    """
    if not _YAML_AVAILABLE:
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    try:
        with open(template_path, encoding="utf-8") as f:
            data = _yaml_module.safe_load(f)
    except Exception:
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    if not isinstance(data, dict):
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    body = data.get("body", [])
    if not isinstance(body, list):
        return TemplateLoadResult(
            required_labels=[],
            error=FAIL_CLOSED_REASON_TEMPLATE_UNAVAILABLE,
        )

    required_labels = []
    for item in body:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            continue
        label = attrs.get("label")
        validations = item.get("validations", {})
        if not isinstance(validations, dict):
            continue
        if label and validations.get("required") is True:
            required_labels.append(label)

    return TemplateLoadResult(required_labels=required_labels, error=None)


def _check_missing_sections_from_template(
    issue_body: str,
    required_labels: list[str],
) -> list[str]:
    """
    Check which required sections (from template labels) are missing in issue body.

    Blocker 4: Uses the heading map from _extract_sections() for exact-match
    detection instead of substring search. This prevents false-positives where
    a label name appears in the body text (not as a ## heading).

    Returns list of missing section labels.
    Skips 'Machine-Readable Contract' label (it's checked separately).
    """
    # Build a set of known heading titles from the body (fence-aware)
    sections = _extract_sections(issue_body)
    present_headings = set(sections.keys())

    missing = []
    for label in required_labels:
        # Machine-Readable Contract is validated separately
        if label == "Machine-Readable Contract":
            continue
        # Exact-match against heading map (Blocker 4: no partial match)
        if label not in present_headings:
            missing.append(label)
    return missing


def _validate_input_schema(data: Any) -> bool:
    """Validate input matches REFINEMENT_LOOP_PLANNER_INPUT_V1 schema."""
    if not isinstance(data, dict):
        return False

    required_fields = ["schema_version", "issue"]
    for field in required_fields:
        if field not in data:
            return False

    if data.get("schema_version") != "refinement_loop_planner_input/v1":
        return False

    issue = data.get("issue", {})
    issue_required = ["number", "title", "body", "labels"]
    for field in issue_required:
        if field not in issue:
            return False

    return True


def _create_evidence_span(
    source: str, text: str, start_line: int, end_line: int, source_ref: Optional[str] = None
) -> dict[str, Any]:
    """Create an EvidenceSpan object."""
    return {
        "source": source,
        "source_ref": source_ref,
        "start_line": start_line,
        "end_line": end_line,
        "text_sha256": _sha256(text),
    }


def _stable_sort_dedupe(items: list[str]) -> list[str]:
    """Sort items stably and dedupe."""
    return sorted(set(items))


def _extract_missing_contract_keys(issue_body: str) -> list[str]:
    """
    Extract missing required contract keys from the Machine-Readable Contract.

    Returns a list of required keys that are absent from the parsed contract dict.
    If the Machine-Readable Contract section exists but cannot be parsed, returns
    all required keys so downstream rewrite constraints can flag contract parse gaps.
    If the section is absent, returns an empty list and leaves section detection
    to a separate caller path.
    """
    if not _YAML_AVAILABLE:
        return []

    sections = _extract_sections(issue_body)
    # Only check for missing keys if a Machine-Readable Contract section exists
    if "Machine-Readable Contract" not in sections:
        return []

    machine_contract = _extract_machine_contract(issue_body)
    if machine_contract is None:
        # Contract section exists but YAML couldn't be parsed — schema parse error
        # Return all required keys as missing so the caller knows what to add
        return list(REQUIRED_CONTRACT_KEYS)

    missing = []
    for key in REQUIRED_CONTRACT_KEYS:
        if key not in machine_contract or machine_contract[key] is None:
            missing.append(key)
    return missing



def _check_contract_parse_error(issue_body: str) -> "str | None":
    """
    AC2: Check whether the Machine-Readable Contract section has a YAML parse error.

    Precondition: the 'Machine-Readable Contract' section MUST exist in the body
    (caller is responsible for checking this first).

    Returns:
      - FAIL_CLOSED_REASON_MALFORMED_CONTRACT if no YAML block found in section
      - FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR if YAML is present but cannot
        be parsed OR the parsed result is not a dict
      - None if YAML is parseable and returns a dict (keys may still be missing)
    """
    if not _YAML_AVAILABLE:
        return FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR

    sections = _extract_sections(issue_body)
    contract_section = sections.get("Machine-Readable Contract", "")

    # No YAML block at all → malformed
    yaml_match = re.search(r"```yaml\n([\s\S]*?)\n```", contract_section)
    if not yaml_match:
        return FAIL_CLOSED_REASON_MALFORMED_CONTRACT

    yaml_content = yaml_match.group(1)
    try:
        parsed = _yaml_module.safe_load(yaml_content)
    except Exception:
        return FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR

    if not isinstance(parsed, dict):
        return FAIL_CLOSED_REASON_CONTRACT_SCHEMA_PARSE_ERROR

    return None


def _strict_json_loads(text: str) -> object:
    """
    AC5: Strict JSON loading that rejects NaN / Infinity / -Infinity.

    Python's json.loads accepts NaN and Infinity by default (parse_constant).
    This function uses a parse_constant hook to reject them explicitly.
    """

    def _reject_nan(value: str) -> float:
        raise ValueError(
            f"Strict JSON reject: NaN/Infinity/−Infinity values are not allowed. "
            f"Got: {value!r}"
        )

    # Python 3.8+ json.loads does NOT expose parse_constant in a portable way.
    # The most portable approach: decode then verify no float nan/inf survived.
    result = json.loads(text)
    _check_no_nan(result)
    return result


def _check_no_nan(obj: object) -> None:
    """AC5: Recursively reject NaN/Infinity in a decoded JSON structure."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError(
                f"Strict JSON reject: NaN/Infinity value encountered: {obj!r}"
            )
    elif isinstance(obj, dict):
        for v in obj.values():
            _check_no_nan(v)
    elif isinstance(obj, list):
        for item in obj:
            _check_no_nan(item)


def _build_fail_closed_rewrite_constraints(
    fail_closed_reasons: list[str],
    missing_sections: list[str],
    missing_contract_keys: list[str],
) -> dict[str, Any]:
    """
    Build FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 payload.

    AC1/AC2/AC8: Returned when fail_closed is triggered, to guide issue-author
    rewrite toward deterministic repair instead of freeform editing.
    """
    # Determine which reasons are overridable (AC7)
    overridable_reasons = [
        r for r in fail_closed_reasons
        if r in OVERRIDE_POLICY["allowed_reason_codes"]
    ]
    non_overridable_reasons = [
        r for r in fail_closed_reasons
        if r in OVERRIDE_POLICY["never_override_reason_codes"]
    ]

    return {
        "schema_version": "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1",
        "required_sections": missing_sections,
        "required_contract_keys": missing_contract_keys,
        "rewrite_constraints": {
            "must_add_sections": missing_sections,
            "must_add_contract_keys": missing_contract_keys,
            "freeform_rewrite_forbidden": True,
        },
        "override_policy": {
            "allowed_reason_codes": OVERRIDE_POLICY["allowed_reason_codes"],
            "never_override_reason_codes": OVERRIDE_POLICY["never_override_reason_codes"],
            "overridable_in_current_result": overridable_reasons,
            "non_overridable_in_current_result": non_overridable_reasons,
        },
        "max_rewrite_attempts": 2,
        "no_progress_route": "human_judgment_required",
    }


def plan_refinement_loop(input_data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """
    Main planner function.

    Returns (REFINEMENT_LOOP_PLAN_V1 dict, exit_code).
    exit_code: 0 = success, 2 = invalid input, 3 = internal error.

    B2: Supports optional 'now' parameter for deterministic generated_at timestamp.
    """
    # Validate input schema
    if not _validate_input_schema(input_data):
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": None,
                "issue_body_sha256": None,
                "comments_sha256": None,
                "known_context_sha256": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {},
            "fail_closed": {
                "required": True,
                "reason_codes": [FAIL_CLOSED_REASON_UNKNOWN_SCHEMA],
                "human_message": "Invalid input schema: missing required fields",
            },
        }
        return fail_closed_plan, 2

    try:
        issue = input_data.get("issue", {})
        comments = input_data.get("comments")
        known_context = input_data.get("known_context")

        issue_number = issue.get("number")
        issue_body = issue.get("body", "")
        issue_title = issue.get("title", "")

        # Compute hashes
        issue_body_sha256 = _sha256(issue_body)
        # B4: Handle comments_sha256 - empty list is different from None
        if comments is None:
            comments_sha256 = None
        elif isinstance(comments, list):
            if len(comments) == 0:
                comments_sha256 = _sha256("[]")
            else:
                comments_sha256 = _sha256(_canonical_json(comments))
        else:
            comments_sha256 = None

        known_context_sha256 = _canonical_json(known_context) if known_context else None
        if known_context_sha256:
            known_context_sha256 = _sha256(known_context_sha256)

        # B2: Support optional 'now' parameter for deterministic timestamp
        if "now" in input_data and input_data["now"]:
            generated_at = input_data["now"]
        else:
            generated_at = datetime.now(timezone.utc).isoformat()

        # The planner is the refinement loop's production decision source.
        # If the caller supplies live main-drift facts, emit this decision
        # before any consumer can reuse base-bound snapshot/CI/review data.
        refinement_main_drift = _refinement_main_drift_decision(known_context)

        # Check for malformations
        fail_closed_reasons = []
        # Track missing sections and contract keys for FAIL_CLOSED_REWRITE_CONSTRAINTS_V1
        accumulated_missing_sections: list[str] = []
        accumulated_missing_contract_keys: list[str] = []

        if _check_malformed_contract(issue_body):
            fail_closed_reasons.append(FAIL_CLOSED_REASON_MALFORMED_CONTRACT)

        # Extract machine contract to determine issue_kind and parent_mode
        machine_contract = _extract_machine_contract(issue_body)
        raw_issue_kind = machine_contract.get("issue_kind") if machine_contract else None
        parent_mode = machine_contract.get("parent_mode") if machine_contract else None

        # Check for missing required contract keys (AC2/AC11)
        section_headings = _extract_sections(issue_body)
        has_contract_section = "Machine-Readable Contract" in section_headings
        if not has_contract_section:
            # AC1: Machine-Readable Contract section is absent → missing_required_section,
            # NOT missing_required_contract_key. The section itself is missing.
            accumulated_missing_sections.extend(["Machine-Readable Contract"])
            fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)
        else:
            # AC2: Section present — distinguish parse error from key absence.
            _contract_parse_error = _check_contract_parse_error(issue_body)
            if _contract_parse_error:
                # YAML parse error or malformed block — separate path from missing key.
                fail_closed_reasons.append(_contract_parse_error)
                # required_contract_keys is empty when we can't parse the YAML at all
            else:
                missing_contract_keys = _extract_missing_contract_keys(issue_body)
                if missing_contract_keys:
                    accumulated_missing_contract_keys.extend(missing_contract_keys)
                    fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_CONTRACT_KEY)

        # Apply alias normalization (design→research, tracking→parent, etc.)
        # _normalize_issue_kind returns None for unknown/unresolvable kinds.
        issue_kind = _normalize_issue_kind(raw_issue_kind) if raw_issue_kind else None

        # Determine if this is a parent delivery-rollup (AC1: exempt from Outcome check)
        is_parent_delivery_rollup = (
            issue_kind == "parent" and parent_mode == "delivery-rollup"
        )

        repo_root = _find_repo_root()

        if raw_issue_kind and not is_parent_delivery_rollup:
            # Apply alias normalization: unknown/unresolvable → fail_closed
            if issue_kind is None:
                fail_closed_reasons.append(FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND)
            else:
                # For known non-parent-delivery-rollup issue kinds: use template-derived required sections
                template_path = resolve_issue_template(issue_kind, repo_root)
                if template_path:
                    load_result = load_required_section_labels(template_path)
                    if load_result.error:
                        # Blocker 2: template load failure → fail_closed
                        fail_closed_reasons.append(load_result.error)
                    else:
                        missing = _check_missing_sections_from_template(issue_body, load_result.required_labels)
                        if missing:
                            accumulated_missing_sections.extend(missing)
                            fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)
                else:
                    # Template not found: fall back to Outcome check
                    if _check_missing_outcome(issue_body):
                        accumulated_missing_sections.extend(["Outcome"])
                        fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)
        elif is_parent_delivery_rollup:
            # AC1: parent delivery-rollup — check parent template sections (not Outcome)
            # AC7: Use separate reason_code for missing parent sections
            template_path = resolve_issue_template("parent", repo_root)
            if template_path:
                load_result = load_required_section_labels(template_path)
                if load_result.error:
                    # Blocker 2: template load failure → fail_closed
                    fail_closed_reasons.append(load_result.error)
                else:
                    missing = _check_missing_sections_from_template(issue_body, load_result.required_labels)
                    if missing:
                        accumulated_missing_sections.extend(missing)
                        fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_PARENT_SECTION)
        else:
            # No machine contract or unknown issue_kind: fall back to Outcome check
            if _check_missing_outcome(issue_body):
                accumulated_missing_sections.extend(["Outcome"])
                fail_closed_reasons.append(FAIL_CLOSED_REASON_MISSING_SECTION)

        # Normalize accumulated constraints once all checks complete.
        accumulated_missing_sections = _stable_sort_dedupe(accumulated_missing_sections)
        accumulated_missing_contract_keys = _stable_sort_dedupe(accumulated_missing_contract_keys)

        # Deduplicate reason_codes while preserving stable order.
        if fail_closed_reasons:
            fail_closed_reasons = list(dict.fromkeys(fail_closed_reasons))

        if fail_closed_reasons:
            # Build FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 for AC1/AC2/AC8
            rewrite_constraints = _build_fail_closed_rewrite_constraints(
                fail_closed_reasons,
                accumulated_missing_sections,
                accumulated_missing_contract_keys,
            )
            # B3: Return schema-valid decisions with unknown confidence even in fail_closed
            return (
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "issue_number": issue_number,
                        "issue_body_sha256": issue_body_sha256,
                        "comments_sha256": comments_sha256,
                        "known_context_sha256": known_context_sha256,
                        "generated_at": generated_at,
                    },
                    "decisions": {
                        "investigation_policy": {
                            "required": False,
                            "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                            "target_paths": [],
                            "repo_claims": [],
                            "evidence_spans": [],
                            "confidence": CONFIDENCE_UNKNOWN,
                        },
                        "web_research_policy": {
                            "required": False,
                            "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                            "critical_external_claims": [],
                            "evidence_spans": [],
                            "confidence": CONFIDENCE_UNKNOWN,
                        },
                        "scope_delta_decision": None,
                        "scope_signal_guard": {
                            "triggered": False,
                            "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                            "excluded_by_anchor_reframe": False,
                            "evidence_spans": [],
                        },
                        "delivery_rollup": {
                            "applicable": False,
                            "unmaterialized_slots": [],
                            "evidence_spans": [],
                        },
                        "follow_up_materialization": {
                            "candidates": [],
                        },
                    },
                    "fail_closed": {
                        "required": True,
                        "reason_codes": fail_closed_reasons,
                        "human_message": f"Issue contract malformation detected: {', '.join(fail_closed_reasons)}",
                        "rewrite_constraints": rewrite_constraints,
                    },
                },
                0,
            )

        # Extract evidence
        target_paths = _extract_paths_from_outcome_ac_vc(issue_body)
        repo_claims = _extract_repo_claims(issue_body)
        critical_external_claims = _extract_critical_external_claims(issue_body, comments)
        unmaterialized_slots = _extract_unmaterialized_slots(issue_body)
        follow_up_candidates = _extract_follow_up_candidates(issue_body)

        # Determine investigation policy
        investigation_required = bool(target_paths) or bool(repo_claims)
        investigation_reason = (
            INVESTIGATION_REASON_TARGET_PATHS_PRESENT
            if target_paths
            else (
                INVESTIGATION_REASON_REPO_FACT_CLAIM
                if repo_claims
                else INVESTIGATION_REASON_NO_REPO_FACT
            )
        )

        investigation_evidence = []
        # B6: Add evidence if investigation is required (either from paths OR repo_claims)
        if investigation_required:
            investigation_evidence.append(
                _create_evidence_span(
                    "issue_body",
                    issue_body,  # Use full body for consistent hashing
                    1,
                    len(issue_body.splitlines()),
                    source_ref=None,
                )
            )

        # Determine web research policy
        web_research_required = bool(critical_external_claims)
        web_research_reason = (
            WEB_RESEARCH_REASON_EXTERNAL_SPEC
            if critical_external_claims
            else WEB_RESEARCH_REASON_NO_CLAIM
        )

        web_research_evidence = []
        if critical_external_claims:
            web_research_evidence.append(
                _create_evidence_span(
                    "issue_body",
                    issue_body,  # Use full body for consistent hashing
                    1,
                    len(issue_body.splitlines()),
                    source_ref=None,
                )
            )

        # Compute the scope_signal_delta result ONCE (fail-closed via
        # ScopeSignalDeltaError) and share it between the legacy
        # scope_signal_guard projection and the v2 lane split. A v2 build
        # failure is NOT swallowed: it propagates to the fail-closed
        # internal-error plan instead of silently omitting the artifact.
        scope_signal_delta_result = None
        if known_context and "scope_signal_delta_input" in known_context:
            scope_signal_delta_result = _compute_scope_signal_delta_result(known_context)

        # Determine scope signal guard using _detect_scope_signals
        scope_signal_triggered, scope_signal_reason, scope_signal_evidence = _detect_scope_signals(
            issue_number,
            issue_body,
            known_context,
            precomputed_delta=scope_signal_delta_result,
        )

        # AC1/AC10 (#1090): opt-in SCOPE_SIGNAL_GUARD_DECISION_V2 lane split.
        # Only computed when known_context carries scope_signal_delta_input,
        # since that is the only source of a normalized added-paths list;
        # this keeps all pre-existing golden fixtures byte-identical.
        scope_signal_guard_decision_v2 = None
        if scope_signal_delta_result is not None:
            _projection = scope_signal_delta_result.get("legacy_scope_signal_guard") or {}
            _raw_triggered = bool(_projection.get("triggered"))
            _raw_reason = (
                _projection.get("reason_code", SCOPE_SIGNAL_REASON_NO_SIGNAL)
                if _raw_triggered
                else SCOPE_SIGNAL_REASON_NO_SIGNAL
            )
            _added_paths = (
                (scope_signal_delta_result.get("sections") or {})
                .get("allowed_paths", {})
                .get("added", [])
            )
            scope_signal_guard_decision_v2 = _build_scope_signal_guard_decision_v2(
                _raw_triggered,
                _raw_reason,
                _added_paths,
                known_context,
                issue_number,
                issue,
            )
            if classify_scope_delta_authority is not None and known_context and (
                "scope_delta_authority_evidence" in known_context
            ):
                # PR #1332 review fix (P0/P1): expected_repo is now mandatory
                # for classify_scope_delta_authority's URL/repo cross-check
                # (AC16 hardening) -- reuse the same repo-derivation helper
                # already used for the #1090 scope_delta_approval evidence
                # path so cross-repo spoofed evidence fails closed here too.
                scope_signal_guard_decision_v2["scope_delta_authority"] = classify_scope_delta_authority(
                    known_context.get("scope_delta_authority_evidence"),
                    # A no-op delta must not suppress the separate freeform
                    # authority lane: the classifier validates its evidence
                    # and remains fail-closed for untrusted or ambiguous input.
                    # PR #2083 review fix (P1-2): the outer `if` above already
                    # confirmed "scope_delta_authority_evidence" in known_context,
                    # i.e. evidence *presence*, not its value truthiness, is what
                    # triggers this lane. Gating on `known_context.get(...)`'s
                    # truthiness made a present-but-falsy evidence payload
                    # ({}, [], "", None) combined with a no-op raw delta silently
                    # fall to triggered=False -> not_triggered/implementation_allowed
                    # (fail-open), diverging from the iteration-zero branch below,
                    # which always uses triggered=True once presence is confirmed.
                    triggered=True,
                    target_issue_number=issue_number,
                    base_issue_body_sha256=issue_body_sha256,
                    expected_repo=_expected_repo_for_issue(issue, known_context),
                    # #2086 AC3: forward the caller's read-only-investigation
                    # exact-path inventory (run_refinement_preflight.py /
                    # codebase-investigator-backed orchestrator step) through
                    # known_context so a trusted operator-selected
                    # architecture/workflow-level directive without an exact
                    # backtick path literal can still clear the
                    # expands_allowed_paths boundary. Absent from
                    # known_context in the ordinary case (None is a no-op for
                    # classify_scope_delta_authority).
                    investigation_derived_path_literals=known_context.get(
                        "investigation_derived_path_literals"
                    ),
                )
        elif classify_scope_delta_authority is not None and known_context and (
            "scope_delta_authority_evidence" in known_context
        ):
            # Iteration zero has no immutable *previous* body and must not
            # fabricate one just to enter the legacy delta lane.  Consume the
            # already-normalized trusted directive through the existing v2
            # sidecar instead; the router will request a candidate-body
            # readiness-gated contract update, never implementation go.
            scope_signal_guard_decision_v2 = {
                "schema_version": "SCOPE_SIGNAL_GUARD_DECISION_V2",
                "raw_signal": {"triggered": False, "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL},
                "scope_context": {"path_layer": []},
                "scope_delta_approval": _base_approval_result(),
                "security_sensitive": False,
                "route": SCOPE_ROUTE_NOT_TRIGGERED,
                "scope_delta_authority": classify_scope_delta_authority(
                    known_context.get("scope_delta_authority_evidence"),
                    triggered=True,
                    target_issue_number=issue_number,
                    base_issue_body_sha256=issue_body_sha256,
                    expected_repo=_expected_repo_for_issue(issue, known_context),
                    # #2086 AC3: see the iteration-N call site above for why
                    # this is forwarded through known_context.
                    investigation_derived_path_literals=known_context.get(
                        "investigation_derived_path_literals"
                    ),
                ),
            }

        # #2048: echo the scope-reframe/empty-operations rewrite route
        # alongside scope_delta_decision (opt-in — see
        # _compute_scope_reframe_rewrite_route docstring). `additionalProperties:
        # true` on decisions.scope_delta_decision (schemas/refinement_loop_plan_v1.json)
        # permits this extra key without a schema change.
        _scope_delta_decision_echo = known_context.get("scope_delta_decision") if known_context else None
        _scope_reframe_rewrite_route = _compute_scope_reframe_rewrite_route(known_context)
        if isinstance(_scope_delta_decision_echo, dict) and _scope_reframe_rewrite_route is not None:
            _scope_delta_decision_echo = {
                **_scope_delta_decision_echo,
                "rewrite_route": _scope_reframe_rewrite_route,
            }

        # Build output
        plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": issue_number,
                "issue_body_sha256": issue_body_sha256,
                "comments_sha256": comments_sha256,
                "known_context_sha256": known_context_sha256,
                "generated_at": generated_at,
            },
            "decisions": {
                "investigation_policy": {
                    "required": investigation_required,
                    "reason_code": investigation_reason,
                    "target_paths": _stable_sort_dedupe(list(target_paths)),
                    "repo_claims": _stable_sort_dedupe(repo_claims),
                    "evidence_spans": investigation_evidence,
                    "confidence": CONFIDENCE_DETERMINISTIC if investigation_required else CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": web_research_required,
                    "reason_code": web_research_reason,
                    "critical_external_claims": critical_external_claims,
                    "evidence_spans": web_research_evidence,
                    "confidence": CONFIDENCE_DETERMINISTIC if web_research_required else CONFIDENCE_UNKNOWN,
                },
                "scope_signal_guard": {
                    "triggered": scope_signal_triggered,
                    "reason_code": scope_signal_reason,
                    "excluded_by_anchor_reframe": scope_signal_reason == SCOPE_SIGNAL_REASON_ANCHOR_REFRAME,
                    "evidence_spans": scope_signal_evidence,
                },
                "scope_delta_decision": _scope_delta_decision_echo,
                "delivery_rollup": {
                    "applicable": bool(unmaterialized_slots),
                    "unmaterialized_slots": unmaterialized_slots,
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": follow_up_candidates,
                },
            },
            "fail_closed": {
                "required": False,
                "reason_codes": [],
                "human_message": "",
            },
        }

        if refinement_main_drift is not None:
            plan["decisions"]["main_drift_evidence_epoch"] = refinement_main_drift
            if refinement_main_drift["route"] == "hard_stop":
                plan["fail_closed"] = {
                    "required": True,
                    "reason_codes": [refinement_main_drift["reason_code"]],
                    "human_message": "main drift evidence cannot be safely rebound",
                }

        if scope_signal_guard_decision_v2 is not None:
            plan["scope_signal_guard_decision_v2"] = scope_signal_guard_decision_v2

        # #1677: ISSUE_EXECUTION_DECISION_V1 (relation graph, execution state,
        # downstream_policy).
        #
        # #1873 (bounded review loops): the self-validation call that used to
        # sit here (validate_issue_execution_decision() + a re-validated
        # fallback artifact) was an internal integrity check the orchestrator
        # ran against its own output before the Replay Arbitration step was
        # removed. It is no longer required -- downstream consumers
        # (run_refinement_preflight.py) independently validate
        # issue_execution_decision via the same canonical
        # validate_issue_execution_decision module when they consume it.
        issue_execution_decision = build_issue_execution_decision(
            issue_number, issue_body_sha256, generated_at, known_context
        )
        plan["issue_execution_decision"] = issue_execution_decision

        return plan, 0

    except ScopeSignalDeltaError as e:
        reason_code = getattr(e, "reason_code", FAIL_CLOSED_REASON_AMBIGUOUS_SIGNAL)
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": issue_number if "issue_number" in locals() else None,
                "issue_body_sha256": issue_body_sha256 if "issue_body_sha256" in locals() else None,
                "comments_sha256": comments_sha256 if "comments_sha256" in locals() else None,
                "known_context_sha256": known_context_sha256 if "known_context_sha256" in locals() else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {
                "investigation_policy": {
                    "required": False,
                    "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                    "target_paths": [],
                    "repo_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": False,
                    "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                    "critical_external_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "scope_delta_decision": None,
                "scope_signal_guard": {
                    "triggered": False,
                    "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                    "excluded_by_anchor_reframe": False,
                    "evidence_spans": [],
                },
                "delivery_rollup": {
                    "applicable": False,
                    "unmaterialized_slots": [],
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": [],
                },
            },
            "fail_closed": {
                "required": True,
                "reason_codes": [reason_code],
                "human_message": str(e),
            },
        }
        return fail_closed_plan, 0

    except Exception as e:
        # Internal error - B3: Return schema-valid decisions with unknown confidence
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": issue_number if "issue_number" in locals() else None,
                "issue_body_sha256": issue_body_sha256 if "issue_body_sha256" in locals() else None,
                "comments_sha256": None,
                "known_context_sha256": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {
                "investigation_policy": {
                    "required": False,
                    "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                    "target_paths": [],
                    "repo_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": False,
                    "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                    "critical_external_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "scope_delta_decision": None,
                "scope_signal_guard": {
                    "triggered": False,
                    "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                    "excluded_by_anchor_reframe": False,
                    "evidence_spans": [],
                },
                "delivery_rollup": {
                    "applicable": False,
                    "unmaterialized_slots": [],
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": [],
                },
            },
            "fail_closed": {
                "required": True,
                "reason_codes": [FAIL_CLOSED_REASON_INTERNAL_ERROR],
                "human_message": f"Internal error: {str(e)}",
            },
        }
        return fail_closed_plan, 3


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Main CLI entry point."""
    try:
        input_text = sys.stdin.read()
        input_data = _strict_json_loads(input_text)
    except (json.JSONDecodeError, ValueError) as e:
        # B3: Return schema-valid decisions even for JSON decode errors
        fail_closed_plan = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "issue_number": None,
                "issue_body_sha256": None,
                "comments_sha256": None,
                "known_context_sha256": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "decisions": {
                "investigation_policy": {
                    "required": False,
                    "reason_code": INVESTIGATION_REASON_UNKNOWN_SCHEMA,
                    "target_paths": [],
                    "repo_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "web_research_policy": {
                    "required": False,
                    "reason_code": WEB_RESEARCH_REASON_UNKNOWN_SCHEMA,
                    "critical_external_claims": [],
                    "evidence_spans": [],
                    "confidence": CONFIDENCE_UNKNOWN,
                },
                "scope_delta_decision": None,
                "scope_signal_guard": {
                    "triggered": False,
                    "reason_code": SCOPE_SIGNAL_REASON_NO_SIGNAL,
                    "excluded_by_anchor_reframe": False,
                    "evidence_spans": [],
                },
                "delivery_rollup": {
                    "applicable": False,
                    "unmaterialized_slots": [],
                    "evidence_spans": [],
                },
                "follow_up_materialization": {
                    "candidates": [],
                },
            },
            "fail_closed": {
                "required": True,
                "reason_codes": [FAIL_CLOSED_REASON_UNKNOWN_SCHEMA],
                "human_message": f"Invalid JSON input: {str(e)}",
            },
        }
        print(json.dumps(fail_closed_plan, ensure_ascii=False, indent=2, allow_nan=False))
        sys.exit(2)

    plan, exit_code = plan_refinement_loop(input_data)

    # Output JSON to stdout (no prose/markdown)
    output_text = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False)
    print(output_text)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
