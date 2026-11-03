"""
Production consumer module for impl-review-loop Step 5 routing.

Issue #1873: reviewer self-report of merge_ready / required_auto_actions /
allowed_paths_gate is no longer a production input. pr-reviewer's minimal
result convention is verdict / reviewed_head_sha / blockers / warnings only
(see .claude/skills/pr-review-judge/references/loop-verdict-v2-schema.md and
references/verdict-output-template.md). Mergeability
(mergeable, merge_state_status) is read directly from live GitHub PR state
by the caller and passed in as `live_mergeability`.

Issue #1860 Owner Decision / PR #1871 (#1869): the ONLY hard stop retained by
this router is a real Git conflict / GitHub non-mergeability, and it is
evaluated BEFORE any reviewer_verdict dispatch -- a REQUEST_CHANGES or
HUMAN_REVIEW_REQUIRED verdict does not bypass it. UNKNOWN, BLOCKED, BEHIND,
UNSTABLE, DRAFT, HAS_HOOKS are never treated as a conflict.

GitHub's actual enums (see GraphQL schema):
  PullRequest.mergeable        (MergeableState):  CONFLICTING | MERGEABLE | UNKNOWN
  PullRequest.mergeStateStatus (MergeStateStatus): BEHIND | BLOCKED | CLEAN | DIRTY |
                                                    DRAFT | HAS_HOOKS | UNKNOWN | UNSTABLE

NOTE: "CONFLICTING" is a valid value of `mergeable` but is NOT a valid value
of `merge_state_status` (that enum member does not exist on GitHub). A
live_mergeability payload that sets merge_state_status == "CONFLICTING" is
therefore malformed input, not a legitimate conflict signal, and is rejected
as schema_invalid rather than silently treated as a conflict.

Import-time side effects are forbidden: no gh, git, network, or subprocess
calls are made at module load or function call. Callers are responsible for
fetching reviewer_verdict (from the pr-reviewer SubAgent's returned result)
and live_mergeability (from `gh pr view --json headRefOid,mergeable,
mergeStateStatus`) before calling route_loop_verdict_v2().

Public API
----------
route_loop_verdict_v2(reviewer_verdict, live_mergeability) -> RouteDecision

Issue #2607 (PR review fix_delta iteration 1, P0-1/P1): the preparation.md
pre-dispatch choke point and the already_satisfied_evidence producer are
ALSO production functions of this module -- not helpers re-implemented in a
test file:

resolve_already_satisfied_early_exit_decision(
    *, next_action_route, product_spec_routing_action, pr_exists,
    base_ac_satisfied,
) -> dict
    Pure decision function backing preparation.md's "0-a-1. Already-Satisfied
    Early-Exit choke point". Fires (dispatch_step1=False) iff pr_exists is
    False AND base_ac_satisfied is True, independent of the upstream
    next_action_route / product_spec_routing_action value (AC5/AC12).

build_already_satisfied_evidence(
    *, base_test_verdict, pr_head_test_verdict, live_main_sha,
    live_pr_head_sha,
) -> dict
    Canonical producer (AC13) of already_satisfied_evidence's three values
    (base_ac_satisfied / meaningful_pr_delta / evidence_base_sha) from two
    independent TEST_VERDICT_MACHINE/v2 reports plus the two live SHAs the
    caller already resolved. Callers do not assemble the three values
    themselves.

Issue #1870 (#1856): this function does not accept a ``test_verdict``
argument. BEHIND routing is derived solely from
``live_mergeability["merge_state_status"] == "BEHIND"``. The protected
TEST_VERDICT producer/publisher lane established by #1856 is unaffected by
this module; it simply has no input into ordinary review routing.

Issue #2607: when ``verdict == "REQUEST_CHANGES"``, an OPTIONAL
``live_mergeability["already_satisfied_evidence"]`` key (same optional-key
pattern as ``main_drift``, Issue #2102 -- the public two-input signature
does not change) lets the caller supply independently-derived evidence that
the target Issue's Verification Commands already pass on current main and
that the open PR carries no meaningful delta over that base run:

    already_satisfied_evidence:
      base_ac_satisfied: bool     # independent base-checkout TEST_VERDICT/v2
      meaningful_pr_delta: bool   # PASS-set comparison between base and PR head
      evidence_base_sha: <sha>    # commit the base run was executed against

This router does not compute these facts itself (no subprocess/gh access);
it only compares ``evidence_base_sha`` against ``live_mergeability["main_drift"]
["current_base_sha"]`` (freshness) and requires both booleans plus a real
Git conflict absence. Only when ALL FOUR conditions hold (REQUEST_CHANGES
verdict, base_ac_satisfied is True, meaningful_pr_delta is False,
evidence_base_sha == current main HEAD) does the router select
ROUTE_ALREADY_SATISFIED instead of ROUTE_CONTINUE_LOOP; ``main_drift``
missing (freshness undecidable) or any condition failing falls back to the
ordinary ROUTE_CONTINUE_LOOP -- the pre-#2607 REQUEST_CHANGES behavior is
byte-identical when ``already_satisfied_evidence`` is absent.
ROUTE_ALREADY_SATISFIED is a pure result-structuring route: it never
performs (and this module never gains the ability to perform) any PR/Issue
close mutation itself -- see the module-level "no gh, git, network, or
subprocess calls" invariant above, which this route does not weaken.

RouteDecision fields
---------------------
route:            one of the ROUTE_* constants below
fail_closed:      True when a missing / unknown / mismatched input, or a
                  non-conflict mergeability state outside the router's
                  authority (UNKNOWN / BLOCKED / UNSTABLE / DRAFT), forced a
                  safe-default (non-actionable, non-escalating) outcome.
                  Callers must not treat fail_closed as a process failure --
                  branch on `route` and `reason_code`.
reason_code:      machine-readable short code explaining the outcome, or None
selected_action:  the resolved auto-action (dict), synthesized by this
                  router for the update_branch case, or None
rerun_required:   dict with boolean keys 'verification' and 'pr_review'
errors:           tuple of human-readable error strings (empty on success)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping


# ---------------------------------------------------------------------------
# Route constants
# ---------------------------------------------------------------------------

ROUTE_APPROVED = "approved"
ROUTE_CONTINUE_LOOP = "continue_loop"
ROUTE_ALREADY_SATISFIED = "already_satisfied"
ROUTE_TO_UPDATE_BRANCH = "route_to_update_branch"
ROUTE_SCOPE_CLEAN_RECONCILIATION = "route_scope_clean_reconciliation"
ROUTE_STALE_HEAD_REREVIEW = "route_stale_head_rereview"
ROUTE_HUMAN_ESCALATION = "route_human_escalation"
ROUTE_CONFLICT_HARD_STOP = "conflict_hard_stop"
ROUTE_FAIL_CLOSED = "fail_closed"

# ---------------------------------------------------------------------------
# Canonical update_branch action shape (synthesized by this router; no
# longer accepted as reviewer input per #1873)
# ---------------------------------------------------------------------------

_UPDATE_BRANCH_EXECUTOR = "implementation-worker"
_UPDATE_BRANCH_SKILL = "implement-issue.update_branch"

_VALID_VERDICTS: frozenset[str] = frozenset(
    {
        "APPROVE",
        "REQUEST_CHANGES",
        "HUMAN_REVIEW_REQUIRED",
    }
)

_VALID_MERGEABLE: frozenset[str] = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})

_VALID_MERGE_STATE_STATUS: frozenset[str] = frozenset(
    {
        "CLEAN",
        "UNSTABLE",
        "BEHIND",
        "DIRTY",
        "BLOCKED",
        "UNKNOWN",
        "DRAFT",
        "HAS_HOOKS",
    }
)

# merge_state_status values that require deferral to the current-head
# required-CI / branch-protection evaluator. NOT a conflict, NOT a human
# escalation (#1860 Owner Decision / PR #1871 P0-3).
_DEFER_TO_CI_EVALUATOR_STATUSES: frozenset[str] = frozenset({"BLOCKED", "UNSTABLE", "DRAFT"})

# merge_state_status values compatible with an approved route.
_APPROVABLE_STATUSES: frozenset[str] = frozenset({"CLEAN", "HAS_HOOKS"})

# Legacy/self-reported fields that are no longer accepted as routing input
# (Issue #1873). `test_verdict` is included: PR #1870 (#1856) removed it from
# the public API, and it must not resurface as a reviewer_verdict field.
_REJECTED_LEGACY_FIELDS: tuple[str, ...] = (
    "merge_ready",
    "required_auto_actions",
    "allowed_paths_gate",
    "mergeability",
    "mergeStateStatus",
    "recommendations",
    "test_verdict",
)


# Shared, side-effect-free main-drift evidence policy (Issue #2102).  In
# agent-platform (I-4-4A) the policy lives ONCE in the already-migrated
# scripts/agent-ops/main_drift_policy.py (AST-identical to the definitions that
# were embedded here at the source tag); this module re-uses it instead of
# duplicating it.  An unresolvable policy module is a hard import failure, not
# a silent "no drift" default.
_AGENT_OPS_DIR = Path(__file__).resolve().parents[4] / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))
from main_drift_policy import (  # noqa: E402
    _MAIN_DRIFT_SHA,
    DEFAULT_MAX_DRIFT_REBIND_ATTEMPTS,
    MainDriftDecision,
    classify_main_drift,
)



_STEP5_MAIN_DRIFT_FACTS = (
    "current_base_sha",
    "evidence_base_sha",
    "allowed_paths_snapshot_base_sha",
    "allowed_paths",
    "latest_main_net_diff",
    "expected_old_sha",
    "observed_old_sha",
)


def build_step5_live_mergeability(
    live_pr: Mapping[str, Any],
    main_drift_facts: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the only Step 5 production input shape for main-drift routing.

    The control-plane fetches ``live_pr`` and constructs
    ``main_drift_facts`` from the same decision cycle.  It must not omit
    these facts because the merge state is not ``BEHIND``: CLEAN and
    HAS_HOOKS would otherwise permit stale evidence to reach ``approved``.
    Missing facts are rejected instead of silently treating the decision as
    non-drift.
    """
    if not isinstance(live_pr, Mapping):
        raise ValueError("live_pr must be a mapping")
    if not isinstance(main_drift_facts, Mapping):
        raise ValueError("main_drift_facts must be a mapping")

    missing = [key for key in _STEP5_MAIN_DRIFT_FACTS if key not in main_drift_facts]
    if missing:
        raise ValueError(f"main_drift_facts missing required keys: {', '.join(missing)}")

    result = {
        "head_sha": live_pr.get("head_sha"),
        "mergeable": live_pr.get("mergeable"),
        "merge_state_status": live_pr.get("merge_state_status"),
        "main_drift": dict(main_drift_facts),
    }
    error = _validate_live_mergeability(result)
    if error:
        raise ValueError(error)
    return result


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteDecision:
    route: Literal[
        "approved",
        "continue_loop",
        "already_satisfied",
        "route_to_update_branch",
        "route_scope_clean_reconciliation",
        "route_stale_head_rereview",
        "route_human_escalation",
        "conflict_hard_stop",
        "fail_closed",
    ]
    fail_closed: bool
    reason_code: str | None
    selected_action: Mapping[str, Any] | None
    rerun_required: Mapping[str, bool]
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.selected_action, dict):
            object.__setattr__(self, "selected_action", MappingProxyType(self.selected_action))
        if isinstance(self.rerun_required, dict):
            object.__setattr__(self, "rerun_required", MappingProxyType(self.rerun_required))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NO_RERUN: Mapping[str, bool] = {"verification": False, "pr_review": False}


def _decision(
    route: str,
    *,
    fail_closed: bool = False,
    reason_code: str | None = None,
    selected_action: dict[str, Any] | None = None,
    rerun_required: Mapping[str, bool] = _NO_RERUN,
    errors: tuple[str, ...] = (),
) -> RouteDecision:
    return RouteDecision(
        route=route,
        fail_closed=fail_closed,
        reason_code=reason_code,
        selected_action=selected_action,
        rerun_required=dict(rerun_required),
        errors=errors,
    )


def _fail(reason_code: str, *error_msgs: str) -> RouteDecision:
    return _decision(
        ROUTE_FAIL_CLOSED,
        fail_closed=True,
        reason_code=reason_code,
        errors=tuple(error_msgs),
    )


def _conflict(reason_code: str) -> RouteDecision:
    """The ONE class of hard stop retained by #1860 Owner Decision: real Git
    conflict / GitHub-reported non-mergeability. This is NOT a fail-closed
    safe-default; it is a deterministic, intentional escalation to the
    CONFLICTING PR Escalation Runbook.
    """
    return _decision(ROUTE_CONFLICT_HARD_STOP, reason_code=reason_code)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _validate_reviewer_verdict(raw: Any) -> str | None:
    """Return a reason_code if reviewer_verdict is malformed, else None."""
    if not isinstance(raw, Mapping):
        return "schema_invalid_reviewer_verdict_not_mapping"

    verdict = raw.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return f"schema_invalid_verdict_value:{verdict!r}"

    reviewed_head_sha = raw.get("reviewed_head_sha")
    if not isinstance(reviewed_head_sha, str) or not reviewed_head_sha:
        return "schema_invalid_reviewed_head_sha_empty_or_missing"

    blockers = raw.get("blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(b, str) for b in blockers):
        return "schema_invalid_blockers_not_string_list"

    warnings = raw.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
        return "schema_invalid_warnings_not_string_list"

    # Legacy/self-reported authority fields are no longer accepted (#1873).
    for legacy_key in _REJECTED_LEGACY_FIELDS:
        if legacy_key in raw:
            return f"schema_invalid_legacy_field_present:{legacy_key}"

    return None


def _validate_live_mergeability(raw: Any) -> str | None:
    """Return a reason_code if live_mergeability is malformed, else None."""
    if not isinstance(raw, Mapping):
        return "schema_invalid_live_mergeability_not_mapping"

    head_sha = raw.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return "schema_invalid_live_head_sha_empty_or_missing"

    mergeable = raw.get("mergeable")
    if mergeable not in _VALID_MERGEABLE:
        return f"schema_invalid_mergeable_value:{mergeable!r}"

    merge_state_status = raw.get("merge_state_status")
    if merge_state_status not in _VALID_MERGE_STATE_STATUS:
        # "CONFLICTING" is a valid `mergeable` value but NOT a valid GitHub
        # MergeStateStatus enum member. A payload that puts it here is
        # malformed input, not a legitimate conflict signal.
        return f"schema_invalid_merge_state_status_value:{merge_state_status!r}"

    main_drift_error = _validate_main_drift_facts(raw)
    if main_drift_error:
        return main_drift_error

    return None


def _validate_main_drift_facts(raw: Mapping[str, Any]) -> str | None:
    """Validate main-drift facts IF the caller supplied them.

    main_drift remains an OPTIONAL key on the public two-input routing
    boundary (module docstring; Design Invariant in Issue #2102: the
    public route_loop_verdict_v2(reviewer_verdict, live_mergeability)
    signature does not change). Production callers that have live
    current-main facts available SHOULD use
    build_step5_live_mergeability(), which requires the complete fact set
    up front and raises before this function is ever reached with a partial
    payload. Callers that omit main_drift entirely keep the pre-#2102
    routing behavior (no drift classification is applied). Only a PRESENT
    but malformed main_drift dict is rejected here.
    """
    if "main_drift" not in raw:
        return None

    main_drift = raw["main_drift"]
    if not isinstance(main_drift, Mapping):
        return "main_drift_context_invalid"

    missing = [key for key in _STEP5_MAIN_DRIFT_FACTS if key not in main_drift]
    if missing:
        return "main_drift_context_invalid"

    for key in (
        "current_base_sha",
        "evidence_base_sha",
        "allowed_paths_snapshot_base_sha",
        "expected_old_sha",
        "observed_old_sha",
    ):
        value = main_drift[key]
        if not isinstance(value, str) or not _MAIN_DRIFT_SHA.fullmatch(value):
            return "main_drift_context_invalid"

    allowed_paths = main_drift["allowed_paths"]
    if (
        not isinstance(allowed_paths, (list, tuple))
        or not allowed_paths
        or any(not isinstance(path, str) or not path for path in allowed_paths)
    ):
        return "main_drift_context_invalid"

    latest_main_net_diff = main_drift["latest_main_net_diff"]
    if not isinstance(latest_main_net_diff, (list, tuple)) or any(
        not isinstance(path, str) or not path for path in latest_main_net_diff
    ):
        return "main_drift_context_invalid"

    for key in ("semantic_ambiguity", "behind_fast_path_eligible"):
        if key in main_drift and not isinstance(main_drift[key], bool):
            return "main_drift_context_invalid"

    for int_key in ("drift_rebind_attempts", "max_drift_rebind_attempts"):
        if int_key not in main_drift:
            continue
        value = main_drift[int_key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return "main_drift_context_invalid"
        if int_key == "max_drift_rebind_attempts" and value < 1:
            return "main_drift_context_invalid"

    return None


# ---------------------------------------------------------------------------
# merge_state_status-only BEHIND determination (Issue #1856 AC1/AC2)
# ---------------------------------------------------------------------------
#
# Issue #1856: The historical branch_behind_main / test_verdict cross-check
# depended on the protected TEST_VERDICT lane. Ordinary-review evidence
# authority no longer depends on that lane, so BEHIND is derived solely from
# live_mergeability["merge_state_status"]. This module accepts no
# test_verdict input of any kind.


def _is_behind(merge_state_status: Any) -> bool:
    """Return True iff merge_state_status is exactly the string "BEHIND"."""
    return merge_state_status == "BEHIND"


def _classify_implementation_main_drift(
    main_drift: Mapping[str, Any],
    *,
    reviewed_head_sha: str,
    live_head_sha: str,
    merge_state_status: str,
) -> MainDriftDecision | None:
    """Map caller-supplied live facts into the shared pure policy.

    This is deliberately independent of approval / CI state.  A stale
    base-bound snapshot, CI result, or review may not survive merely because
    GitHub currently reports CLEAN, HAS_HOOKS, BLOCKED, or another state.
    """
    try:
        return classify_main_drift(
            current_base_sha=main_drift["current_base_sha"],
            evidence_base_sha=main_drift["evidence_base_sha"],
            allowed_paths_snapshot_base_sha=main_drift["allowed_paths_snapshot_base_sha"],
            allowed_paths=main_drift["allowed_paths"],
            latest_main_net_diff=main_drift["latest_main_net_diff"],
            expected_head_sha=reviewed_head_sha,
            observed_head_sha=live_head_sha,
            expected_old_sha=main_drift["expected_old_sha"],
            observed_old_sha=main_drift["observed_old_sha"],
            semantic_ambiguity=bool(main_drift.get("semantic_ambiguity", False)),
            behind_fast_path_eligible=(
                _is_behind(merge_state_status) and bool(main_drift.get("behind_fast_path_eligible", False))
            ),
            drift_rebind_attempts=int(main_drift.get("drift_rebind_attempts", 0)),
            max_drift_rebind_attempts=int(
                main_drift.get("max_drift_rebind_attempts", DEFAULT_MAX_DRIFT_REBIND_ATTEMPTS)
            ),
        )
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Issue #2607: already_satisfied terminal route (Step 5 recovery path).
# ---------------------------------------------------------------------------


def _already_satisfied_decision() -> RouteDecision:
    """Build the ROUTE_ALREADY_SATISFIED RouteDecision.

    Structuring only -- see module docstring: this router never performs
    the PR/Issue close mutation itself, only recommends it. `pr.action:
    close` is used here (not `none`) because this route is only reachable
    from the Step 5 recovery path, where the PR already exists (the
    early-exit `pr.action: none` variant belongs to preparation.md's
    pre-dispatch choke point, which runs before any PR/reviewer_verdict
    exists and therefore never calls this router at all).
    """
    return _decision(
        ROUTE_ALREADY_SATISFIED,
        reason_code="already_satisfied_no_meaningful_pr_delta",
        selected_action={
            "kind": "already_satisfied",
            "result": {
                "status": "no_change_required",
                "termination_reason": "already_satisfied",
                "merge_ready": False,
            },
            "recommendation": {
                "pr": {"action": "close", "reason": "no_meaningful_delta"},
                "issue": {
                    "action": "close",
                    "state_reason": "completed",
                    "reason": "requirement_already_delivered",
                },
            },
        },
    )


def _evaluate_already_satisfied(live_mergeability: Mapping[str, Any]) -> RouteDecision | None:
    """Return ROUTE_ALREADY_SATISFIED iff all four AC2 conditions hold given
    a REQUEST_CHANGES verdict, else None (caller falls back to
    ROUTE_CONTINUE_LOOP -- byte-identical to pre-#2607 behavior).

    Deliberately reads ONLY `already_satisfied_evidence` (a caller-supplied
    fact triple) and `main_drift.current_base_sha` (freshness comparator,
    #2102 reuse). It never reads `reviewer_verdict["blockers"]` free text
    (AC4) and never requires `reviewed_head_sha == live head_sha` (AC3 --
    that equality check is specific to the APPROVE stale-head path and is
    not part of this evaluation).
    """
    evidence = live_mergeability.get("already_satisfied_evidence")
    if not isinstance(evidence, Mapping):
        return None

    base_ac_satisfied = evidence.get("base_ac_satisfied")
    meaningful_pr_delta = evidence.get("meaningful_pr_delta")
    evidence_base_sha = evidence.get("evidence_base_sha")

    if base_ac_satisfied is not True or meaningful_pr_delta is not False:
        return None
    if not isinstance(evidence_base_sha, str) or not _MAIN_DRIFT_SHA.fullmatch(evidence_base_sha):
        return None

    # Co-occurrence requirement (Issue #2607 Evidence producers section):
    # main_drift missing means freshness is undecidable -> stale evidence,
    # not eligible (falls back to continue_loop, not a schema error).
    main_drift = live_mergeability.get("main_drift")
    if not isinstance(main_drift, Mapping):
        return None

    current_base_sha = main_drift.get("current_base_sha")
    if evidence_base_sha != current_base_sha:
        return None

    return _already_satisfied_decision()


# ---------------------------------------------------------------------------
# Issue #2699: pre-Step-1 landing disposition. The evidence producer owns
# strict parsing/validation/normalization; this consumer is the pure route.
# ---------------------------------------------------------------------------


def resolve_pre_step1_landing_disposition(
    evidence: Mapping[str, Any] | str | None,
    *,
    repo: str,
    issue_number: int,
) -> dict[str, Any]:
    """Derive a pre-dispatch disposition without I/O or caller assertions."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).with_name("implementation_landed_evidence.py")
    # I-4-4A: implementation_landed_evidence.py is NOT migrated yet (owner: I-4-4C).
    # At the source tag a missing file raised an unhandled FileNotFoundError at
    # exec_module(); an absent producer must instead yield the documented
    # fail-closed disposition (never ordinary dispatch / already_satisfied).
    spec = (
        importlib.util.spec_from_file_location("implementation_landed_evidence_for_route", path)
        if path.is_file()
        else None
    )
    if spec is None or spec.loader is None:
        return {
            "disposition": "reconciliation_required",
            "reason_codes": ["implementation_landed_evidence_module_unavailable"],
            "candidate": None,
        }
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.derive_landing_disposition(
        evidence,
        repo=repo,
        issue_number=issue_number,
    )


def resolve_pre_step1_data_plane_action(
    disposition: Mapping[str, Any],
) -> dict[str, Any]:
    """The enforceable pre-Step-1 gate used before any worker/worktree/PR call.

    `disposition` MUST already be a resolved landing-disposition mapping
    (`{"disposition": <name>, "reason_codes": [...], "candidate": ...}`) --
    either `derive_landing_disposition()`'s / `resolve_pre_step1_landing_
    disposition()`'s direct output, or, after Disposition Precedence
    composition (#2607/#2713),
    `implementation_landed_evidence.py::apply_already_satisfied_precedence()`'s
    output (which may replace the disposition name with `already_satisfied`).

    #2713 AC5: this function performs no evidence parsing/validation/
    recompute of its own anymore -- it used to accept raw `evidence` and
    internally call `resolve_pre_step1_landing_disposition()` again, which
    meant a caller that had already composed a final disposition (e.g.
    `already_satisfied` overriding a bare
    `ordinary_dispatch_or_explicit_recovery`) could have that composition
    silently undone by an independent second raw-evidence reinterpretation
    here. Taking an already-resolved `disposition` mapping instead makes
    that double-application structurally impossible: there is exactly one
    place (the caller) that derives a disposition, and exactly one call
    into this function to turn it into a data-plane action.

    Keeping this mapping in the production route module makes the
    suppression more than preparation prose: callers receive a single
    `start_data_plane` boolean and may not reinterpret the disposition
    themselves.
    """
    name = disposition.get("disposition") if isinstance(disposition, Mapping) else None
    if name == "ordinary_dispatch_or_explicit_recovery":
        return {"disposition": disposition, "start_data_plane": True, "action": "dispatch_step1"}
    if name == "existing_pr_resume":
        return {"disposition": disposition, "start_data_plane": False, "action": "resume_existing_pr"}
    return {"disposition": disposition, "start_data_plane": False, "action": "suppress_worker_worktree_new_pr"}


# ---------------------------------------------------------------------------
# Issue #2607 fix_delta iteration 1 (PR #2626 review comment, P0-1): the
# preparation.md pre-dispatch choke point's decision logic is a production
# function of THIS module, not a helper duplicated inside a test file. Both
# .claude/skills/impl-review-loop/tests/test_already_satisfied_routing.py
# and test_already_satisfied_early_exit_e2e_runtime_only.py import this
# function rather than re-defining it, so unit-level and real-subprocess
# e2e coverage exercise the exact same single source of truth.
# ---------------------------------------------------------------------------


def resolve_already_satisfied_early_exit_decision(
    *,
    next_action_route: str,
    product_spec_routing_action: str,
    pr_exists: bool,
    base_ac_satisfied: bool,
) -> dict[str, Any]:
    """Pure decision function for preparation.md's `0-a-1` choke point.

    Fires (dispatch_step1=False) iff `pr_exists` is False AND
    `base_ac_satisfied` is True -- regardless of `next_action_route` /
    `product_spec_routing_action` (AC5: single common choke point,
    independent of which upstream branch value is currently driving Step 1
    continuation).
    """
    if pr_exists or not base_ac_satisfied:
        return {
            "early_exit": False,
            "dispatch_step1": True,
            "reason": "pr_already_exists" if pr_exists else "base_ac_not_satisfied",
            "upstream_route": next_action_route,
            "upstream_product_spec_routing_action": product_spec_routing_action,
        }
    return {
        "early_exit": True,
        "dispatch_step1": False,
        "reason": "already_satisfied_no_pr_created",
        "result": {
            "status": "no_change_required",
            "termination_reason": "already_satisfied",
            "merge_ready": False,
        },
        "recommendation": {
            "pr": {"action": "none", "reason": "no_pr_created"},
            "issue": {
                "action": "close",
                "state_reason": "completed",
                "reason": "requirement_already_delivered",
            },
        },
        "upstream_route": next_action_route,
        "upstream_product_spec_routing_action": product_spec_routing_action,
    }


# ---------------------------------------------------------------------------
# Issue #2607 fix_delta iteration 1 (PR #2626 review comment, P1): canonical
# producer of already_satisfied_evidence's three values (AC13). Callers
# supply two independent TEST_VERDICT_MACHINE/v2 reports (test-runner's
# existing read-only report contract, .claude/agents/test-runner.md) plus
# the two live SHAs they already resolved (live main HEAD via the
# fetch_live_issue()-style `gh api .../git/refs/heads/<base_ref>` pattern,
# and the live PR head SHA) -- this function performs no subprocess / gh /
# git access of its own (module-level "no gh, git, network, or subprocess
# calls" invariant, module docstring) and derives the three values rather
# than letting a caller assemble them freely.
# ---------------------------------------------------------------------------


def _pass_ac_ids(verdict: Any) -> frozenset[str] | None:
    """Return the set of AC ids with status == "pass" in a
    TEST_VERDICT_MACHINE/v2's `runtime_ac_results` (`.claude/agents/
    test-runner.md`'s `runtime_ac_results[].ac` field), or None if the
    report is malformed/empty and therefore cannot be trusted for a
    PASS-set comparison."""
    if not isinstance(verdict, Mapping):
        return None
    results = verdict.get("runtime_ac_results")
    if not isinstance(results, list) or not results:
        return None
    ac_ids: set[str] = set()
    for entry in results:
        if not isinstance(entry, Mapping):
            return None
        ac_id = entry.get("ac")
        status = entry.get("status")
        if not isinstance(ac_id, str) or not ac_id:
            return None
        if status == "pass":
            ac_ids.add(ac_id)
        elif status not in ("fail", "skip"):
            return None
    return frozenset(ac_ids)


def build_already_satisfied_evidence(
    *,
    base_test_verdict: Mapping[str, Any],
    pr_head_test_verdict: Mapping[str, Any],
    live_main_sha: str,
    live_pr_head_sha: str,
) -> dict[str, Any]:
    """Derive already_satisfied_evidence's three values (AC13).

    base_ac_satisfied:
        True only when `base_test_verdict["head_sha"] == live_main_sha`
        (the base run was actually executed against the SHA the caller just
        resolved as current live main -- not some earlier/stale checkout)
        AND every entry in its `runtime_ac_results` has `status: pass`.
        A stale/mismatched base run fails closed to False rather than being
        silently trusted.

    meaningful_pr_delta:
        PASS-set comparison between `base_test_verdict` and
        `pr_head_test_verdict`'s `runtime_ac_results` (AC4 -- never a
        `blockers[]` free-text heuristic). Fails closed to True (assume a
        delta exists) when the PR-head report is stale
        (`pr_head_test_verdict["head_sha"] != live_pr_head_sha`) or either
        report is malformed/empty, since an untrustworthy comparison must
        never suppress `already_satisfied`.

    evidence_base_sha:
        Always `live_main_sha` -- the live main SHA the caller resolved via
        the canonical `gh api repos/<repo>/git/refs/heads/<base_ref>
        --jq '.object.sha'` pattern (see preparation.md), not a value this
        function invents.
    """
    base_ac_satisfied = (
        isinstance(base_test_verdict, Mapping)
        and base_test_verdict.get("head_sha") == live_main_sha
        and _pass_ac_ids(base_test_verdict) is not None
        and all(
            isinstance(entry, Mapping) and entry.get("status") == "pass"
            for entry in base_test_verdict.get("runtime_ac_results", [])
        )
    )

    pr_head_fresh = (
        isinstance(pr_head_test_verdict, Mapping) and pr_head_test_verdict.get("head_sha") == live_pr_head_sha
    )
    base_pass_ids = _pass_ac_ids(base_test_verdict)
    pr_pass_ids = _pass_ac_ids(pr_head_test_verdict) if pr_head_fresh else None

    if base_pass_ids is None or pr_pass_ids is None:
        meaningful_pr_delta = True
    else:
        meaningful_pr_delta = base_pass_ids != pr_pass_ids

    return {
        "base_ac_satisfied": bool(base_ac_satisfied),
        "meaningful_pr_delta": bool(meaningful_pr_delta),
        "evidence_base_sha": live_main_sha,
    }


def _reconciliation_decision(drift: MainDriftDecision) -> RouteDecision:
    return _decision(
        ROUTE_SCOPE_CLEAN_RECONCILIATION,
        reason_code=drift.reason_code,
        selected_action={
            "kind": "scope_clean_reconciliation",
            "evidence_epoch": dict(drift.evidence_epoch or {}),
            "reusable_evidence": dict(drift.reusable_evidence),
        },
        rerun_required=drift.reverify,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route_loop_verdict_v2(
    reviewer_verdict: Mapping[str, Any],
    live_mergeability: Mapping[str, Any],
) -> RouteDecision:
    """Deterministic, side-effect-free routing for impl-review-loop Step 5.

    Parameters
    ----------
    reviewer_verdict:
        Minimal result convention returned by the pr-reviewer SubAgent:
        {verdict, reviewed_head_sha, blockers, warnings}.
    live_mergeability:
        Live GitHub PR state fetched by the caller:
        {head_sha, mergeable, merge_state_status}.  When current-main facts
        are available, the caller adds them under ``main_drift`` (optional
        key; see build_step5_live_mergeability()) so the public two-input
        routing boundary remains stable. A malformed (but present)
        main_drift payload fails closed; an absent main_drift key falls
        back to pre-#2102 routing (no drift classification).

    Returns
    -------
    RouteDecision

    Routing priority (Issue #1873 / #1860 Owner Decision / PR #1871)
    -------------------------------------------------------------------
    1. Structural validation of both inputs.
    2. Actual conflict (mergeable == "CONFLICTING" or
       merge_state_status == "DIRTY") is evaluated BEFORE any reviewer
       verdict is inspected, and hard-stops regardless of verdict.
    3. Reviewer verdict dispatch (HUMAN_REVIEW_REQUIRED / REQUEST_CHANGES /
       APPROVE). For REQUEST_CHANGES, an already_satisfied_evidence check
       (Issue #2607) runs first: only when base_ac_satisfied is True,
       meaningful_pr_delta is False, and evidence_base_sha matches
       live_mergeability["main_drift"]["current_base_sha"] does this select
       ROUTE_ALREADY_SATISFIED instead of ROUTE_CONTINUE_LOOP.
    4. For APPROVE: blockers must be empty; a stale reviewed_head_sha routes
       to re-review rather than dispatching any action; otherwise live
       mergeability determines approved / update_branch / deferred-to-CI /
       unknown-pending-retry.

    Note (Issue #1856, AC1/AC2)
    ----------------------------
    BEHIND routing is derived solely from
    ``live_mergeability["merge_state_status"] == "BEHIND"``; the historical
    branch_behind_main cross-check against the protected TEST_VERDICT lane
    has been removed (evidence authority cutover, Phase 1).
    """
    err = _validate_reviewer_verdict(reviewer_verdict)
    if err:
        return _fail(err, f"reviewer_verdict schema error: {err}")

    err = _validate_live_mergeability(live_mergeability)
    if err:
        return _fail(err, f"live_mergeability schema error: {err}")

    verdict_str: str = reviewer_verdict["verdict"]
    reviewed_head_sha: str = reviewer_verdict["reviewed_head_sha"]
    blockers: list[str] = list(reviewer_verdict.get("blockers", []))

    live_head_sha: str = live_mergeability["head_sha"]
    mergeable: str = live_mergeability["mergeable"]
    merge_state_status: str = live_mergeability["merge_state_status"]

    # ------------------------------------------------------------------
    # Priority 2: actual Git conflict, evaluated BEFORE verdict dispatch.
    # Applies regardless of verdict (#1860 Owner Decision / PR #1871 P0-2):
    # a CONFLICTING/DIRTY PR must hard-stop even under REQUEST_CHANGES or
    # HUMAN_REVIEW_REQUIRED.
    # ------------------------------------------------------------------
    if mergeable == "CONFLICTING":
        return _conflict("conflict_mergeable_CONFLICTING")

    if merge_state_status == "DIRTY":
        return _conflict("conflict_merge_state_status_DIRTY")

    # Main drift is an implementation-loop transition, rather than a BEHIND
    # special case.  Rebind/invalidate stale base-bound evidence before any
    # reviewer verdict or merge-state dispatch can reuse it.  A real Git
    # conflict remains the higher-priority terminal route above.
    drift_strategy: str | None = None
    main_drift = live_mergeability.get("main_drift")
    if main_drift is not None:
        drift = _classify_implementation_main_drift(
            main_drift,
            reviewed_head_sha=reviewed_head_sha,
            live_head_sha=live_head_sha,
            merge_state_status=merge_state_status,
        )
        if drift is None:
            return _fail("main_drift_context_invalid", "main drift context is incomplete")
        if drift.route == "hard_stop":
            return _fail(
                drift.reason_code or "main_drift_hard_stop",
                "main drift reconciliation is unsafe",
            )
        if drift.route == "scope_clean_reconciliation":
            return _reconciliation_decision(drift)
        if drift.route == "behind_fast_path":
            drift_strategy = "behind_fast_path"

    # ------------------------------------------------------------------
    # Priority 3: reviewer verdict dispatch.
    # ------------------------------------------------------------------
    if verdict_str == "HUMAN_REVIEW_REQUIRED":
        return _decision(
            ROUTE_HUMAN_ESCALATION,
            reason_code="reviewer_human_review_required",
        )

    if verdict_str == "REQUEST_CHANGES":
        already_satisfied = _evaluate_already_satisfied(live_mergeability)
        if already_satisfied is not None:
            return already_satisfied
        return _decision(ROUTE_CONTINUE_LOOP)

    # verdict_str == "APPROVE" from here on (validated above).

    if blockers:
        return _fail(
            "approve_with_blockers_inconsistent",
            "verdict is APPROVE but blockers is non-empty; treat as an "
            "inconsistent reviewer result rather than routing.",
        )

    # ------------------------------------------------------------------
    # Stale reviewed_head_sha -> re-review at current head, not a hard stop.
    # No mutation/action is ever dispatched from a stale APPROVE.
    # ------------------------------------------------------------------
    if reviewed_head_sha != live_head_sha:
        return _decision(
            ROUTE_STALE_HEAD_REREVIEW,
            reason_code="stale_reviewed_head_sha",
        )

    # ------------------------------------------------------------------
    # UNKNOWN mergeable/merge_state_status: not a conflict, not a human
    # escalation. Caller performs a bounded retry, then records a warning
    # and holds only merge-readiness (not the review outcome) as pending.
    # ------------------------------------------------------------------
    if mergeable == "UNKNOWN" or merge_state_status == "UNKNOWN":
        return _fail(
            "mergeability_unknown",
            f"mergeable={mergeable}, merge_state_status={merge_state_status}: "
            f"not yet resolved by GitHub; caller should bounded-retry, then "
            f"record a warning without escalating to a human.",
        )

    # ------------------------------------------------------------------
    # BEHIND: synthesize the update_branch action. The reviewer no longer
    # supplies required_auto_actions; this router computes it from live
    # state per #1873.
    # ------------------------------------------------------------------
    if _is_behind(merge_state_status):
        action = {
            "kind": "update_branch",
            "executor": _UPDATE_BRANCH_EXECUTOR,
            "skill": _UPDATE_BRANCH_SKILL,
            "mechanical": True,
            "expected_head_sha": reviewed_head_sha,
        }
        if drift_strategy is not None:
            action["main_drift_strategy"] = drift_strategy
        return _decision(
            ROUTE_TO_UPDATE_BRANCH,
            selected_action=action,
            rerun_required={"verification": True, "pr_review": True},
        )

    # ------------------------------------------------------------------
    # BLOCKED / UNSTABLE / DRAFT: not a conflict, not a human escalation
    # (#1860 Owner Decision / PR #1871 P0-3). The router has no CI/branch
    # protection input, so it defers -- the orchestrator consults the
    # current-head required-CI / branch-protection evaluator and treats
    # this as a warning, not a stop condition. DRAFT specifically must
    # never trigger human escalation on its own: Issue #1873's Delivery
    # Rule requires the PR to stay Draft pending human final merge.
    # ------------------------------------------------------------------
    if merge_state_status in _DEFER_TO_CI_EVALUATOR_STATUSES:
        return _fail(
            f"merge_state_status_{merge_state_status.lower()}_not_conflict_defer_to_ci_evaluator",
            f"merge_state_status={merge_state_status} is not a Git conflict; "
            f"defer to the current-head required-CI / branch-protection "
            f"evaluator rather than escalating to a human.",
        )

    # ------------------------------------------------------------------
    # Approvable: CLEAN or HAS_HOOKS with MERGEABLE. HAS_HOOKS is not a
    # conflict and does not block approval.
    # ------------------------------------------------------------------
    if merge_state_status in _APPROVABLE_STATUSES and mergeable == "MERGEABLE":
        return _decision(ROUTE_APPROVED)

    return _fail(
        "unexpected_mergeability_combination",
        f"mergeable={mergeable}, merge_state_status={merge_state_status} did not match any known routing branch.",
    )


# ---------------------------------------------------------------------------
# Issue #2102 fix_delta iteration 5, Blocker B: caller-side convenience
# wrapper for the git-conflict-detection oracle.
# ---------------------------------------------------------------------------
#
# route_loop_verdict_v2() above stays pure / side-effect-free (module
# docstring Design Invariant: no gh, git, network, or subprocess calls at
# module load or function call). It therefore continues to accept
# main_drift["semantic_ambiguity"] as a caller-supplied boolean when
# present, exactly as before this Issue.
#
# scripts/agent-ops/pr_head_replay_publish_exec.py is the implementation
# loop's existing subprocess-capable Allowed Paths exact file: this wrapper
# imports its compute_semantic_ambiguity() (a thin public export of the same
# git merge-tree --write-tree oracle _merge_tree_conflicts() already uses for
# the publish executor's own conflict check) rather than adding a new file
# or granting route_loop_verdict_v2.py subprocess authority of its own.
#
# This is option (a) from the Issue #2102 fix_delta iteration 5 contract:
# the router's own pure API is unchanged; a sibling function in the same
# Allowed Paths exact file supplies the missing fact for callers that did
# not already resolve it deterministically upstream (e.g. via
# build_step5_live_mergeability(), which still accepts a caller-supplied
# main_drift["semantic_ambiguity"] when the caller already computed it).
def route_loop_verdict_v2_resolve_semantic_ambiguity(
    reviewer_verdict: Mapping[str, Any],
    live_mergeability: Mapping[str, Any],
    *,
    cwd: Any,
) -> RouteDecision:
    """Resolve main_drift["semantic_ambiguity"] via the real Git
    merge-tree oracle before dispatching to the pure route_loop_verdict_v2().

    If ``live_mergeability["main_drift"]`` is absent, malformed, or already
    carries an explicit ``semantic_ambiguity`` boolean, this delegates to
    ``route_loop_verdict_v2()`` unchanged (byte-identical routing to a
    direct call). Only when main_drift is a well-formed mapping supplying
    ``current_base_sha``/``evidence_base_sha`` but omitting
    ``semantic_ambiguity`` does this wrapper perform the one Git subprocess
    call (``git merge-tree --write-tree``, via
    pr_head_replay_publish_exec.compute_semantic_ambiguity()) needed to fill
    it in deterministically, never accepting a caller-asserted boolean as a
    substitute.
    """
    main_drift = live_mergeability.get("main_drift") if isinstance(live_mergeability, Mapping) else None
    if (
        isinstance(main_drift, Mapping)
        and "semantic_ambiguity" not in main_drift
        and isinstance(main_drift.get("current_base_sha"), str)
        and isinstance(main_drift.get("evidence_base_sha"), str)
        and main_drift["current_base_sha"] != main_drift["evidence_base_sha"]
    ):
        import sys as _sys_for_semantic_ambiguity_import
        from pathlib import Path as _Path_for_semantic_ambiguity_import

        _agent_ops_dir = _Path_for_semantic_ambiguity_import(__file__).resolve().parents[4] / "scripts" / "agent-ops"
        if str(_agent_ops_dir) not in _sys_for_semantic_ambiguity_import.path:
            _sys_for_semantic_ambiguity_import.path.insert(0, str(_agent_ops_dir))
        try:
            from pr_head_replay_publish_exec import compute_semantic_ambiguity as _compute_semantic_ambiguity
        except ImportError:
            _compute_semantic_ambiguity = None

        if _compute_semantic_ambiguity is None:
            # I-4-4A: the git merge-tree oracle (pr_head_replay_publish_exec.py) is NOT
            # migrated yet (owner: I-4-4C).  At the source tag this fell through to
            # route_loop_verdict_v2() with semantic_ambiguity treated as False
            # (fail-open: a possibly-conflicting drift could reach reconciliation).
            # Without the oracle the ambiguity is undecidable -> fail closed.
            return _fail(
                "semantic_ambiguity_oracle_unavailable",
                "git merge-tree oracle (pr_head_replay_publish_exec.compute_semantic_ambiguity)"
                " is unavailable; main-drift semantic ambiguity cannot be resolved",
            )

        if _compute_semantic_ambiguity is not None:
            resolved_ambiguity = _compute_semantic_ambiguity(
                main_drift["evidence_base_sha"],
                main_drift["current_base_sha"],
                cwd=cwd,
            )
            live_mergeability = dict(live_mergeability)
            live_mergeability["main_drift"] = {
                **main_drift,
                "semantic_ambiguity": bool(resolved_ambiguity),
            }

    return route_loop_verdict_v2(reviewer_verdict, live_mergeability)
