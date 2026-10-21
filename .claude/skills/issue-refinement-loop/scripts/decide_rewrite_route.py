#!/usr/bin/env python3
"""
decide_rewrite_route.py

Pure function that routes the rewrite loop based on router state, enforcing:
- max_rewrite_attempts limit
- body hash no-progress detection
- missing set no-progress detection
- checker_exit_code gating (only route to review/handoff when exit_code == 0)

Schema: LOOP_REWRITE_ROUTER_STATE_V1
Output: RouteResult

Usage (as library):
    from decide_rewrite_route import decide_rewrite_route, LOOP_REWRITE_ROUTER_STATE_V1

    state = LOOP_REWRITE_ROUTER_STATE_V1(...)
    result = decide_rewrite_route(state)

Exit codes (CLI):
    0 - success
    2 - invalid input schema
    3 - internal error
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

# ---------------------------------------------------------------------------
# LOOP_REWRITE_ROUTER_STATE_V1 schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "loop_rewrite_router_state/v1"

# Route constants
ROUTE_CONTINUE_REWRITE = "continue_rewrite"
ROUTE_PROCEED_TO_REVIEW = "proceed_to_review"
ROUTE_HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"
ROUTE_CATEGORY_WIDE_REMEDIATION = "category_wide_remediation"
# #2048 regression: routes for the approved-scope-reframe / empty-operations
# router (decide_scope_reframe_contract_route below). ROUTE_CONTRACT_UPDATE
# is the normal outcome (operations[] is non-empty, a section-bound patch
# plan can be built). ROUTE_ISSUE_EDITOR_REQUIRED is the full-body-rewrite
# fallback used when operations[] is empty despite an approved scope reframe.
ROUTE_CONTRACT_UPDATE = "contract_update"
ROUTE_ISSUE_EDITOR_REQUIRED = "issue_editor_required"

# Reason codes
REASON_CODE_MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
REASON_CODE_BODY_HASH_UNCHANGED = "body_hash_unchanged"
REASON_CODE_MISSING_CONTRACT_NO_PROGRESS = "missing_contract_no_progress"
REASON_CODE_CHECKER_PASSED = "checker_passed"
REASON_CODE_CHECKER_FAILED = "checker_failed_rewrite"
REASON_CODE_SOURCE_BODY_RESET = "source_body_reset"
REASON_CODE_REPEATED_FIX_CATEGORY_REMEDIATION = (
    "repeated_fix_category_remediation"
)
REASON_CODE_FIX_CATEGORY_UNDECIDABLE = "fix_category_undecidable"
REASON_CODE_FINGERPRINT_DUPLICATE = "rewrite_request_fingerprint_duplicate"
# #2048 regression (follow-up to #1877/PR #1884): an approved scope reframe
# (approved_by_trusted_anchor + non-empty allowed_path_deltas) whose derived
# operations[] is empty requires a full contract/body rewrite; routing this
# case to contract_update would produce a no-progress retry loop because
# there is no section-bound patch to apply.
REASON_CODE_APPROVED_SCOPE_REQUIRES_FULL_CONTRACT_REWRITE = (
    "approved_scope_requires_full_contract_rewrite"
)
REASON_CODE_NULL = None


# ---------------------------------------------------------------------------
# LOOP_REWRITE_ROUTER_STATE_V1 dataclass
# ---------------------------------------------------------------------------


@dataclass
class LOOP_REWRITE_ROUTER_STATE_V1:
    """
    State contract for the rewrite loop router.

    Fields:
        rewrite_attempt_count: Number of completed rewrite attempts (0-indexed).
            0 means no rewrite has been attempted yet.
            This is incremented BEFORE calling decide_rewrite_route.
            max=2: attempt 0/1 are permitted, attempt 2 triggers human_judgment_required.

        max_rewrite_attempts: Maximum number of rewrite attempts allowed.

        checker_exit_code: Exit code from the most recent checker run (post-mutation).
            0 = passed, non-zero = failed.

        checked_body_sha256: SHA256 of the issue body after the most recent rewrite.
            Used for no-progress detection.

        missing_sections: List of missing required sections after most recent check.
            Used for no-progress detection (must monotonically decrease).

        missing_contract_keys: List of missing required contract keys after most recent check.
            Used for no-progress detection (must monotonically decrease).

        fix_category: Current checker failure category used for category-level routing.

        rewrite_history: Ordered history of observed fix categories.

        occurrence_count: Number of occurrences of fix_category in rewrite_history.

        previous_checked_body_sha256: SHA256 of the issue body from the PREVIOUS iteration.
            None if this is the first iteration.

        previous_missing_sections: Missing sections from the PREVIOUS iteration.
            Used to detect lack of progress.

        previous_missing_contract_keys: Missing contract keys from the PREVIOUS iteration.
            Used to detect lack of progress.

        source_issue_body_sha256: SHA256 of the original issue body (before any rewrites).
            Used to detect if the human has changed the source body (reset condition).

        replay_safe: If True, this state has been restored from persistent storage
            and is safe to replay (attempt count has not been reset to 0).

        source_body_reset: True if this state was produced by a reset because the
            human changed the source issue body (source_issue_body_sha256 mismatch
            at load time). When True, rewrite_attempt_count has been reset to 0 and
            the route result records the reset fact (AC7).

        rewrite_request_fingerprint: AC9: SHA256 fingerprint of fix_category + missing sets.
            Used to detect same-fingerprint recurrence. Not persisted to JSON schema.

        previous_rewrite_request_fingerprints: AC9: history of all observed fingerprints
            in this session. If current fingerprint is in this list, route to
            human_judgment_required (no_progress). Not persisted to JSON schema.

        last_mutation_kind: AC10: Kind of mutation applied in this iteration.
            "format_only_repair" does not consume budget. Not persisted to JSON schema.

        budget_debit: AC10: 0 for format_only_repair, 1 otherwise.
            Not persisted to JSON schema.
    """

    rewrite_attempt_count: int
    max_rewrite_attempts: int
    checker_exit_code: int
    checked_body_sha256: str
    missing_sections: list[str]
    missing_contract_keys: list[str]
    fix_category: str = "unknown_contract_failure"
    rewrite_history: list[str] = field(default_factory=list)
    occurrence_count: int = 0
    previous_checked_body_sha256: Optional[str] = None
    previous_missing_sections: list[str] = field(default_factory=list)
    previous_missing_contract_keys: list[str] = field(default_factory=list)
    source_issue_body_sha256: Optional[str] = None
    replay_safe: bool = False
    source_body_reset: bool = False
    # AC9: sha256 fingerprint of strict-JSON (fix_category + missing sets).
    # Used to detect same-fingerprint recurrence → human_judgment/no_progress.
    # NOTE: not persisted to JSON schema (schema has additionalProperties: false).
    rewrite_request_fingerprint: Optional[str] = None
    # AC9: history of observed fingerprints for duplicate detection.
    # NOTE: not persisted to JSON schema (in-memory only).
    previous_rewrite_request_fingerprints: list[str] = field(default_factory=list)
    # AC10: kind of mutation applied in this iteration.
    # "format_only_repair" → budget_debit=0 (does not consume max_iterations).
    # NOTE: not persisted to JSON schema.
    last_mutation_kind: str = "semantic_rewrite"
    # AC10: 0 if last_mutation_kind=="format_only_repair", else 1.
    # NOTE: not persisted to JSON schema.
    budget_debit: int = 1


# ---------------------------------------------------------------------------
# RouteResult
# ---------------------------------------------------------------------------


@dataclass
class RouteResult:
    """
    Terminal result from decide_rewrite_route.

    Fields:
        route: One of ROUTE_CONTINUE_REWRITE, ROUTE_PROCEED_TO_REVIEW,
               ROUTE_HUMAN_JUDGMENT_REQUIRED.

        reason_code: Why this route was chosen. None when checker passed
               and continuing normally.

        rewrite_attempt_count: Echo of the attempt count from state.

        max_rewrite_attempts: Echo of the max attempts from state.

        checked_body_sha256: Echo of checked body SHA from state.

        checker_exit_code: Echo of checker exit code from state.

        missing_sections: Echo of missing sections from state.

        missing_contract_keys: Echo of missing contract keys from state.

        source_body_reset: True if the source body changed and state was reset.
    """

    route: str
    reason_code: Optional[str]
    rewrite_attempt_count: int
    max_rewrite_attempts: int
    checked_body_sha256: str
    checker_exit_code: int
    missing_sections: list[str]
    missing_contract_keys: list[str]
    fix_category: str = "unknown_contract_failure"
    rewrite_history: list[str] = field(default_factory=list)
    occurrence_count: int = 0
    repeated_fix_category: Optional[str] = None
    remaining_blockers: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    suggested_repair_strategy: Optional[str] = None
    stop_reason_if_unrepairable: Optional[str] = None
    source_body_reset: bool = False
    # AC9: fingerprint echo for audit/loop state
    rewrite_request_fingerprint: Optional[str] = None
    # AC10: mutation kind and budget echo
    last_mutation_kind: str = "semantic_rewrite"
    budget_debit: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (AC5 terminal result fields)."""
        return {
            "schema_version": "route_result/v1",
            "route": self.route,
            "reason_code": self.reason_code,
            "rewrite_attempt_count": self.rewrite_attempt_count,
            "max_rewrite_attempts": self.max_rewrite_attempts,
            "checked_body_sha256": self.checked_body_sha256,
            "checker_exit_code": self.checker_exit_code,
            "missing_sections": self.missing_sections,
            "missing_contract_keys": self.missing_contract_keys,
            "fix_category": self.fix_category,
            "rewrite_history": self.rewrite_history,
            "occurrence_count": self.occurrence_count,
            "repeated_fix_category": self.repeated_fix_category,
            "remaining_blockers": self.remaining_blockers,
            "required_evidence": self.required_evidence,
            "suggested_repair_strategy": self.suggested_repair_strategy,
            "stop_reason_if_unrepairable": self.stop_reason_if_unrepairable,
            "source_body_reset": self.source_body_reset,
            "rewrite_request_fingerprint": self.rewrite_request_fingerprint,
            "last_mutation_kind": self.last_mutation_kind,
            "budget_debit": self.budget_debit,
        }


# ---------------------------------------------------------------------------
# Helper: normalize missing sets for no-progress detection
# ---------------------------------------------------------------------------


def _normalize_set(items: list[str]) -> frozenset[str]:
    """
    Normalize a list of missing items to a frozenset for comparison.

    AC4: missing set comparison uses sort + unique + exact match (via frozenset).
    """
    return frozenset(items)


def _missing_universe(
    sections: list[str], keys: list[str]
) -> frozenset[tuple[str, str]]:
    """
    Build a single tagged universe of all missing items.

    Sections and contract keys are namespaced with a tag so that they never
    collide and are compared together as one set. This lets no-progress
    detection treat "one section resolved but one contract key newly missing"
    as NO progress, instead of letting per-category OR logic pass it.
    """
    return frozenset(
        [("section", s) for s in sections] + [("contract_key", k) for k in keys]
    )


def _strictly_decreased(
    current_sections: list[str],
    current_keys: list[str],
    previous_sections: list[str],
    previous_keys: list[str],
) -> bool:
    """
    Return True only if the combined missing universe strictly shrank.

    Progress (monotonic decrease) requires the current missing universe to be a
    PROPER SUBSET of the previous one. This means:
      - every still-missing item was already missing before (no replacements), AND
      - at least one previously-missing item is now resolved.

    A replacement (e.g. previous {A, B} -> current {C}) is NOT progress, because
    {C} is not a subset of {A, B}. A same-size or grown set is NOT progress.

    AC4: comparison is set-based (sort + unique + exact match via frozenset),
    not length-based.
    """
    current = _missing_universe(current_sections, current_keys)
    previous = _missing_universe(previous_sections, previous_keys)
    return current < previous  # strict (proper) subset


_REPAIRABLE_FIX_CATEGORIES = {
    "missing_section",
    "missing_contract_key",
    "unknown_contract_failure",
}


def _remaining_blockers(
    missing_sections: list[str],
    missing_contract_keys: list[str],
) -> list[str]:
    """Build deterministic blocker list in stable order."""
    return [*missing_sections, *missing_contract_keys]


def _required_evidence_for_fix_category(category: str) -> list[str]:
    if category == "missing_section":
        return [
            "カテゴリ内で不足しているセクションを一括で補完する",
            "各セクションへの実装方針と根拠を追加する",
        ]
    if category == "missing_contract_key":
        return [
            "カテゴリ内で欠落した contract key を一括で補完する",
            "machine-readable block の欠損キーを明示する",
        ]
    return [
        "カテゴリ再発を示す blockers と直近の checker 結果を同梱する",
        "カテゴリ横断の修正後に再検証可能な根拠を提示する",
    ]


def _suggested_repair_strategy(category: str) -> str:
    if category == "missing_section":
        return "missing_section 再発時は同カテゴリ全件を一括追加する"
    if category == "missing_contract_key":
        return "missing_contract_key 再発時は契約キー群を同時解消する"
    return "カテゴリ再発を単一カテゴリ改善として一括再修正する"


def _is_repairable_fix_category(category: str) -> bool:
    return category in _REPAIRABLE_FIX_CATEGORIES


def _human_stop_reason(category: str) -> str:
    return (
        f"カテゴリ {category} は現行ルール集合で structured remediation が未定義（"
        "category-wide remediation strategy 未定義）"
    )


# ---------------------------------------------------------------------------
# decide_rewrite_route — pure function (AC1)
# ---------------------------------------------------------------------------



def _make_route_result(state: "LOOP_REWRITE_ROUTER_STATE_V1", **kwargs: Any) -> "RouteResult":
    """AC9/AC10: Helper to create RouteResult with new fingerprint/mutation fields from state."""
    return RouteResult(
        rewrite_request_fingerprint=state.rewrite_request_fingerprint,
        last_mutation_kind=state.last_mutation_kind,
        budget_debit=state.budget_debit,
        **kwargs,
    )


def decide_rewrite_route(state: LOOP_REWRITE_ROUTER_STATE_V1) -> RouteResult:
    """
    Route the rewrite loop based on current router state.

    Priority order:
    1. checker_exit_code == 0 → proceed_to_review (overrides all stop guards)
    2. max_rewrite_attempts exceeded → human_judgment_required
    3. AC9: fingerprint duplicate detected → human_judgment_required
    4. body hash unchanged → human_judgment_required (body_hash_unchanged)
    5. missing set not decreased → human_judgment_required (missing_contract_no_progress)
    6. checker_exit_code != 0 → continue_rewrite (checker_failed_rewrite)

    AC1: checker_exit_code == 0 takes priority over ALL stop guards including
        max_rewrite_attempts, body_hash_unchanged, and missing_contract_no_progress.
        When checker approves, route directly to proceed_to_review regardless of
        budget exhaustion or no-progress conditions.

    AC2: max_rewrite_attempts runtime enforcement (only when checker_exit_code != 0).
        rewrite_attempt_count >= max_rewrite_attempts → human_judgment_required
        off-by-one: max=2, attempt 0/1 allowed, attempt 2 → stop.

    AC4: no-progress detection — body hash + missing set (only when checker_exit_code != 0).

    AC5: terminal result includes all required fields.

    AC7: source_issue_body_sha256 mismatch is a reset condition. When the state
    was produced by such a reset, state.source_body_reset is True and the fact is
    propagated to every RouteResult so the reset is observable in the terminal
    result (not silently swallowed at load time).

    AC9: fingerprint duplicate detection — if rewrite_request_fingerprint is in
    previous_rewrite_request_fingerprints, route to human_judgment_required with
    reason_code rewrite_request_fingerprint_duplicate. This catches the case where
    the rewrite loop keeps producing the same underlying failure without change.
    """

    source_body_reset = state.source_body_reset
    repeated_fix_category = state.fix_category
    remaining_blockers = _remaining_blockers(
        state.missing_sections,
        state.missing_contract_keys,
    )
    required_evidence = _required_evidence_for_fix_category(state.fix_category)
    suggested_repair_strategy = _suggested_repair_strategy(state.fix_category)
    stop_reason_if_unrepairable = (
        _human_stop_reason(state.fix_category) if state.occurrence_count >= 2 else None
    )

    # --- AC1: checker_exit_code == 0 overrides all stop guards ---
    # checker approve takes priority over max_rewrite_attempts, body_hash_unchanged,
    # and missing_contract_no_progress. This is the highest-priority branch.
    if state.checker_exit_code == 0:
        return RouteResult(
            route=ROUTE_PROCEED_TO_REVIEW,
            reason_code=REASON_CODE_CHECKER_PASSED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # From here on: checker_exit_code != 0 (checker did not approve)

    # --- AC3: same-category recurrence should be handled before budget/no-progress gates ---
    if state.occurrence_count >= 2:
        if _is_repairable_fix_category(state.fix_category):
            return RouteResult(
                route=ROUTE_CATEGORY_WIDE_REMEDIATION,
                reason_code=REASON_CODE_REPEATED_FIX_CATEGORY_REMEDIATION,
                rewrite_attempt_count=state.rewrite_attempt_count,
                max_rewrite_attempts=state.max_rewrite_attempts,
                checked_body_sha256=state.checked_body_sha256,
                checker_exit_code=state.checker_exit_code,
                missing_sections=state.missing_sections,
                missing_contract_keys=state.missing_contract_keys,
                fix_category=state.fix_category,
                rewrite_history=state.rewrite_history,
                occurrence_count=state.occurrence_count,
                repeated_fix_category=repeated_fix_category,
                remaining_blockers=remaining_blockers,
                required_evidence=required_evidence,
                suggested_repair_strategy=suggested_repair_strategy,
                stop_reason_if_unrepairable=stop_reason_if_unrepairable,
                source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit
            )

        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_FIX_CATEGORY_UNDECIDABLE,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # --- AC2: max_rewrite_attempts enforcement ---
    # AC10: format_only_repair (budget_debit=0) does not consume max_iterations.
    # Treat effective attempt count as: rewrite_attempt_count - (1 - budget_debit)
    # i.e. if this invocation was format_only_repair, it doesn't count against budget.
    # AC10: budget_debit=0 for format_only_repair → doesn't consume max_iterations
    # Effective attempt count subtracts the free repair if this was format_only_repair.
    effective_attempt_count = state.rewrite_attempt_count - (1 - state.budget_debit)
    if effective_attempt_count >= state.max_rewrite_attempts:
        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_MAX_ATTEMPTS_EXCEEDED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # --- AC9: fingerprint-based convergence detection ---
    # If the same rewrite_request_fingerprint recurs in previous_rewrite_request_fingerprints,
    # the rewrite loop is cycling without progress — route to human_judgment_required.
    if (
        state.rewrite_request_fingerprint is not None
        and state.rewrite_request_fingerprint in state.previous_rewrite_request_fingerprints
    ):
        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_FINGERPRINT_DUPLICATE,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # --- AC4: no-progress detection (only applicable after first iteration) ---
    # Check body hash unchanged
    if (
        state.previous_checked_body_sha256 is not None
        and state.checked_body_sha256 == state.previous_checked_body_sha256
    ):
        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_BODY_HASH_UNCHANGED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # Check missing set no-progress (body changed but missing set did not strictly shrink)
    if state.previous_checked_body_sha256 is not None:
        previous_universe = _missing_universe(
            state.previous_missing_sections, state.previous_missing_contract_keys
        )
        # Only evaluate progress when there was something missing to resolve.
        # If nothing was missing previously, a body change is fine — continue.
        if len(previous_universe) > 0:
            progressed = _strictly_decreased(
                state.missing_sections,
                state.missing_contract_keys,
                state.previous_missing_sections,
                state.previous_missing_contract_keys,
            )
            # No progress if the combined missing universe did not strictly shrink.
            # This catches: same set, grown set, AND replacement (one resolved /
            # another newly missing), and the cross-category case where sections
            # shrink but contract keys grow.
            if not progressed:
                return RouteResult(
                    route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
                    reason_code=REASON_CODE_MISSING_CONTRACT_NO_PROGRESS,
                    rewrite_attempt_count=state.rewrite_attempt_count,
                    max_rewrite_attempts=state.max_rewrite_attempts,
                    checked_body_sha256=state.checked_body_sha256,
                    checker_exit_code=state.checker_exit_code,
                    missing_sections=state.missing_sections,
                    missing_contract_keys=state.missing_contract_keys,
                    fix_category=state.fix_category,
                    rewrite_history=state.rewrite_history,
                    occurrence_count=state.occurrence_count,
                    repeated_fix_category=repeated_fix_category,
                    remaining_blockers=remaining_blockers,
                    required_evidence=required_evidence,
                    suggested_repair_strategy=suggested_repair_strategy,
                    stop_reason_if_unrepairable=stop_reason_if_unrepairable,
                    source_body_reset=source_body_reset,
                # AC9/AC10: echo new state fields
                rewrite_request_fingerprint=state.rewrite_request_fingerprint,
                last_mutation_kind=state.last_mutation_kind,
                budget_debit=state.budget_debit
                )

    # checker_exit_code != 0 and within budget — continue rewrite loop
    return RouteResult(
        route=ROUTE_CONTINUE_REWRITE,
        reason_code=REASON_CODE_CHECKER_FAILED,
        rewrite_attempt_count=state.rewrite_attempt_count,
        max_rewrite_attempts=state.max_rewrite_attempts,
        checked_body_sha256=state.checked_body_sha256,
        checker_exit_code=state.checker_exit_code,
        missing_sections=state.missing_sections,
        missing_contract_keys=state.missing_contract_keys,
        fix_category=state.fix_category,
        rewrite_history=state.rewrite_history,
        occurrence_count=state.occurrence_count,
        repeated_fix_category=repeated_fix_category,
        remaining_blockers=remaining_blockers,
        required_evidence=required_evidence,
        suggested_repair_strategy=suggested_repair_strategy,
        stop_reason_if_unrepairable=stop_reason_if_unrepairable,
        source_body_reset=source_body_reset,
    # AC9/AC10: echo new state fields
    rewrite_request_fingerprint=state.rewrite_request_fingerprint,
    last_mutation_kind=state.last_mutation_kind,
    budget_debit=state.budget_debit
    )


# ---------------------------------------------------------------------------
# #2048 regression: approved scope reframe with empty operations[] router
# ---------------------------------------------------------------------------
#
# Follow-up to #1877/PR #1884 (trusted anchor patch plan consumer). When a
# scope reframe has been approved by a trusted anchor
# (scope_delta_status == "approved_by_trusted_anchor") AND carries a
# non-empty allowed_path_deltas, but the derived CONTRACT_PATCH_PLAN_V1
# operations[] is empty (no section-bound append operation could be
# derived -- a full body rewrite is required instead), routing to
# contract_update would keep re-issuing a no-progress patch-plan mutation
# against the same Issue body / anchor comment. This router instead routes
# to issue_editor_required (reason_code:
# approved_scope_requires_full_contract_rewrite) so the full-body-rewrite
# path (issue-editor skill) is used, and de-dupes both the no-progress
# contract_update retry (AC2) and the scope-reframe comment post (AC3) via
# a stable empty_operations_fingerprint.


@dataclass
class SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1:
    """
    Input state for decide_scope_reframe_contract_route().

    Fields:
        scope_delta_status: Status string from SCOPE_DELTA_AUTHORITY_V1 /
            scope_signal_guard_decision_v2 (e.g. "approved_by_trusted_anchor").

        allowed_path_deltas: The Allowed Paths delta list attached to the
            approved scope reframe. Non-empty means the reframe expanded /
            constrained the Allowed Paths in a way that requires a contract
            update route (as opposed to a no-op replay).

        operations: The CONTRACT_PATCH_PLAN_V1 operations[] list derived by
            scope_signal_delta.derive_contract_patch_operations(). Empty
            means no section-bound append operation could be derived and a
            full body rewrite is required.

        anchor_comment_url: The trusted anchor comment URL that approved the
            scope reframe. Part of the no-progress / duplicate-comment
            fingerprint identity.

        issue_body_sha256: SHA256 of the current Issue body. Part of the
            fingerprint identity so a fresh body edit invalidates prior
            no-progress suppression.

        previous_empty_operations_fingerprints: History of
            empty_operations_fingerprint values already observed for this
            anchor/body-sha combination (AC2 no-progress retry detection).

    Note (PR #2057 OWNER review P1-2): this route never posts a
    scope-reframe comment (`comment_action` on the result is always
    "none"), so there is no comment-fingerprint history field here.
    """

    scope_delta_status: str
    allowed_path_deltas: list = field(default_factory=list)
    operations: list = field(default_factory=list)
    anchor_comment_url: Optional[str] = None
    issue_body_sha256: Optional[str] = None
    previous_empty_operations_fingerprints: list = field(default_factory=list)


@dataclass
class ScopeReframeRouteResult:
    """Terminal result from decide_scope_reframe_contract_route()."""

    route: str
    reason_code: Optional[str]
    empty_operations_fingerprint: Optional[str]
    # AC2: True when this exact fingerprint has already been seen -- callers
    # MUST NOT re-issue a no-progress contract_update command in this case.
    no_progress_retry_suppressed: bool
    # P1-2 (PR #2057 OWNER review): this transition never posts a
    # scope-reframe comment to the Issue.  A prior `should_post_comment` /
    # `duplicate_comment` pair depended on fingerprint history the
    # production caller never threaded through
    # (`posted_scope_reframe_comment_fingerprints` was test-only), so
    # `should_post_comment: true` was reachable on a fresh process and
    # contradicted AC3 ("no new scope-reframe comment"). `comment_action` is
    # unconditionally "none" -- callers MUST NOT post a comment for this
    # route. If replay-safe comment suppression is needed in the future, it
    # must be re-added backed by persisted transaction-identity state (see
    # Issue #2048 PR #2057 review), not an in-memory optional list.
    comment_action: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "scope_reframe_route_result/v1",
            "route": self.route,
            "reason_code": self.reason_code,
            "empty_operations_fingerprint": self.empty_operations_fingerprint,
            "no_progress_retry_suppressed": self.no_progress_retry_suppressed,
            "comment_action": self.comment_action,
        }


_APPROVED_SCOPE_REFRAME_STATUS = "approved_by_trusted_anchor"


def compute_empty_operations_fingerprint(
    anchor_comment_url: Optional[str],
    issue_body_sha256: Optional[str],
) -> str:
    """
    AC2: Deterministic fingerprint identifying a specific
    (anchor_comment_url, issue_body_sha256) empty-operations occurrence.

    Two invocations with the same anchor comment and the same Issue body
    SHA256 always produce the same fingerprint, so a re-run of the loop
    against an unchanged Issue body/anchor can be recognized as a
    no-progress retry (AC2) and its scope-reframe comment recognized as a
    duplicate (AC3) rather than posted again.
    """
    payload = json.dumps(
        {
            "anchor_comment_url": anchor_comment_url,
            "issue_body_sha256": issue_body_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decide_scope_reframe_contract_route(
    state: SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1,
) -> ScopeReframeRouteResult:
    """
    AC1: route an approved scope reframe with empty operations[] to
    issue_editor_required instead of contract_update.

    An approved scope reframe is defined as
    scope_delta_status == "approved_by_trusted_anchor" AND a non-empty
    allowed_path_deltas. When operations[] is also empty (no section-bound
    patch plan could be derived), a full body rewrite is required and this
    function returns ROUTE_ISSUE_EDITOR_REQUIRED with reason_code
    approved_scope_requires_full_contract_rewrite (AC1).

    AC2: the returned empty_operations_fingerprint is stable for a given
    (anchor_comment_url, issue_body_sha256) pair. When that fingerprint is
    already present in previous_empty_operations_fingerprints,
    no_progress_retry_suppressed is True -- callers must NOT re-issue a
    no-progress contract_update command for this occurrence.

    AC3 (revised, PR #2057 OWNER review P1-2): this route never posts a
    scope-reframe comment. `comment_action` is always "none" on the returned
    result -- see `ScopeReframeRouteResult.comment_action` docstring.

    In every other case (not an approved scope reframe, or operations[] is
    non-empty), this function returns ROUTE_CONTRACT_UPDATE with
    reason_code None -- the normal section-bound patch-plan path is
    unaffected (#1877's existing implementation is preserved).
    """
    is_approved_scope_reframe = (
        state.scope_delta_status == _APPROVED_SCOPE_REFRAME_STATUS
        and bool(state.allowed_path_deltas)
    )
    empty_operations = not state.operations

    if not (is_approved_scope_reframe and empty_operations):
        return ScopeReframeRouteResult(
            route=ROUTE_CONTRACT_UPDATE,
            reason_code=None,
            empty_operations_fingerprint=None,
            no_progress_retry_suppressed=False,
        )

    empty_operations_fingerprint = compute_empty_operations_fingerprint(
        state.anchor_comment_url, state.issue_body_sha256
    )
    no_progress_retry_suppressed = (
        empty_operations_fingerprint in state.previous_empty_operations_fingerprints
    )

    return ScopeReframeRouteResult(
        route=ROUTE_ISSUE_EDITOR_REQUIRED,
        reason_code=REASON_CODE_APPROVED_SCOPE_REQUIRES_FULL_CONTRACT_REWRITE,
        empty_operations_fingerprint=empty_operations_fingerprint,
        no_progress_retry_suppressed=no_progress_retry_suppressed,
    )


# ---------------------------------------------------------------------------
# PR #2057 OWNER REQUEST_CHANGES (P0-1): single canonical scope-reframe
# disposition classifier.
#
# Before this, `invalid` input (malformed operations/allowed_path_deltas,
# anchor binding mismatch, parser/import failure, an unavailable patch-plan
# producer) had no distinct observable outcome: it silently collapsed into
# either `blocked` (consumer) or ordinary `no_change` (via
# `_extract_validated_scope_delta_deltas()` returning `None` on ANY failure,
# valid or invalid alike), and the planner echo coerced a non-list
# `operations` to `[]` instead of failing closed. This function is now the
# ONLY place that decides `patch` / `proven_no_change` / `full_rewrite_
# required` / `invalid`; both the trusted-anchor consumer
# (`run_refinement_preflight.consume_trusted_anchor_contract_patch_plan`)
# and the read-only planner echo
# (`plan_refinement_loop._compute_scope_reframe_rewrite_route`) call this
# SAME function on the SAME inputs so they cannot disagree (regression test:
# planner/consumer parity).
# ---------------------------------------------------------------------------

Disposition = Literal["patch", "proven_no_change", "full_rewrite_required", "invalid"]

REASON_INVALID_PATCH_PLAN_PRODUCER_UNAVAILABLE = "invalid_patch_plan_producer_unavailable"
REASON_INVALID_OPERATIONS_KEY_MISSING = "invalid_operations_key_missing"
REASON_INVALID_OPERATIONS_NOT_LIST = "invalid_operations_not_list"
REASON_INVALID_ALLOWED_PATH_DELTAS_NOT_LIST = "invalid_allowed_path_deltas_not_list"
REASON_INVALID_BLANK_DELTA = "invalid_blank_delta_entry"
REASON_INVALID_ANCHOR_BINDING_MISMATCH = "invalid_anchor_binding_mismatch"
REASON_INVALID_SOURCE_EVIDENCE_BINDING_MISMATCH = "invalid_source_evidence_binding_mismatch"
REASON_INVALID_ALLOWED_PATHS_PARSER_UNAVAILABLE = "invalid_allowed_paths_parser_unavailable"
REASON_NOT_SCOPE_REFRAME = "not_scope_reframe"
REASON_PROVEN_NO_CHANGE = "scope_reframe_deltas_already_reflected"
REASON_PATCH_OPERATIONS_DERIVED = "scope_reframe_operations_derived"


@dataclass(frozen=True)
class ScopeReframeDecision:
    """Terminal, transaction-local classification result.

    `route`/`empty_operations_fingerprint`/`no_progress_retry_suppressed`/
    `comment_action` are only meaningful for `disposition in
    {"patch", "full_rewrite_required"}` derived from an approved scope
    reframe; they are `None`/defaults for `proven_no_change` (no route
    decision needed -- nothing to write) and `invalid` (fail closed, no
    route at all -- `route` is always `None` for `invalid`).
    """

    disposition: Disposition
    reason_code: str
    route: Optional[str] = None
    empty_operations_fingerprint: Optional[str] = None
    no_progress_retry_suppressed: bool = False
    comment_action: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "scope_reframe_decision/v1",
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "route": self.route,
            "empty_operations_fingerprint": self.empty_operations_fingerprint,
            "no_progress_retry_suppressed": self.no_progress_retry_suppressed,
            "comment_action": self.comment_action,
        }


def classify_scope_reframe_disposition(
    *,
    scope_delta_status: Optional[str],
    operations_key_present: bool,
    operations: Any,
    allowed_path_deltas_raw: Any,
    anchor_binding_ok: bool = True,
    patch_plan_producer_available: bool = True,
    source_evidence_binding_ok: bool = True,
    reflected_status: str = "invalid_or_unavailable",
    anchor_comment_url: Optional[str] = None,
    issue_body_sha256: Optional[str] = None,
    previous_empty_operations_fingerprints: Optional[list] = None,
) -> ScopeReframeDecision:
    """Single canonical pure-function scope-reframe disposition classifier.

    Args (all caller-supplied facts; this function does no I/O and performs
    no re-classification of already-computed evidence):
        scope_delta_status: raw status string from the scope-delta decision
            (e.g. `SCOPE_DELTA_AUTHORITY_EVIDENCE_V1.route` /
            `scope_signal_guard_decision_v2`), or `None`/absent.
        operations_key_present: True iff the patch-plan-like object the
            caller is classifying actually HAS an `operations` key (as
            opposed to a caller synthesizing `[]` because no plan exists at
            all -- callers MUST NOT set this True for a synthesized-from-
            absence value).
        operations: the raw `operations` value (must be a `list` to be
            valid; anything else, including `None`, is `invalid`).
        allowed_path_deltas_raw: the raw (unvalidated)
            `allowed_path_deltas` value. `None` means "no scope-reframe
            delta at all" (ordinary patch-plan consumer, not a scope
            reframe). A non-`list` non-`None` value is `invalid`. Any
            blank/whitespace-only string entry is `invalid`.
        anchor_binding_ok: False when the anchor URL/hash/trust binding the
            caller independently verified does NOT match this transaction
            (anchor drift / TOCTOU) -> `invalid`.
        patch_plan_producer_available: False when the patch-plan producer
            (parser/import) failed or is unavailable -> `invalid`. This is
            the ONLY channel through which "no patch plan could be
            produced" reaches this function; it must never be inferred by
            synthesizing `operations=[]` from an absent plan.
        source_evidence_binding_ok: False when the source evidence bound to
            the plan does not match the anchor/body this transaction is
            classifying -> `invalid`.
        reflected_status: `"present"` | `"absent"` | `"invalid_or_unavailable"`
            -- the tri-state Allowed Paths postcondition result (see
            `run_refinement_preflight._check_scope_reframe_deltas_reflected`).
            `"invalid_or_unavailable"` (parser failure, or not yet computed)
            is `invalid`, never silently treated as `"absent"`.

    Returns a frozen `ScopeReframeDecision`. `invalid` is reachable ONLY
    through explicit, named preconditions above -- never as a fallback for
    an unrecognized combination (any input this function does not
    explicitly classify as `invalid`, `proven_no_change`, or
    `full_rewrite_required` is `patch`, matching the pre-#2048 no-op-safe
    default of leaving the ordinary section-bound patch-plan path
    untouched).
    """
    if not patch_plan_producer_available:
        return ScopeReframeDecision("invalid", REASON_INVALID_PATCH_PLAN_PRODUCER_UNAVAILABLE)
    if not operations_key_present:
        return ScopeReframeDecision("invalid", REASON_INVALID_OPERATIONS_KEY_MISSING)
    if not isinstance(operations, list):
        return ScopeReframeDecision("invalid", REASON_INVALID_OPERATIONS_NOT_LIST)
    if allowed_path_deltas_raw is not None and not isinstance(allowed_path_deltas_raw, list):
        return ScopeReframeDecision("invalid", REASON_INVALID_ALLOWED_PATH_DELTAS_NOT_LIST)

    normalized_deltas: list[str] = []
    if isinstance(allowed_path_deltas_raw, list):
        for item in allowed_path_deltas_raw:
            if not isinstance(item, str) or not item.strip():
                return ScopeReframeDecision("invalid", REASON_INVALID_BLANK_DELTA)
            normalized_deltas.append(item)

    if not anchor_binding_ok:
        return ScopeReframeDecision("invalid", REASON_INVALID_ANCHOR_BINDING_MISMATCH)
    if not source_evidence_binding_ok:
        return ScopeReframeDecision("invalid", REASON_INVALID_SOURCE_EVIDENCE_BINDING_MISMATCH)

    is_approved_scope_reframe = (
        scope_delta_status == _APPROVED_SCOPE_REFRAME_STATUS and bool(normalized_deltas)
    )
    if not is_approved_scope_reframe:
        return ScopeReframeDecision("patch", REASON_NOT_SCOPE_REFRAME, route=ROUTE_CONTRACT_UPDATE)

    if reflected_status == "invalid_or_unavailable":
        return ScopeReframeDecision("invalid", REASON_INVALID_ALLOWED_PATHS_PARSER_UNAVAILABLE)
    if reflected_status == "present":
        return ScopeReframeDecision("proven_no_change", REASON_PROVEN_NO_CHANGE, route=ROUTE_CONTRACT_UPDATE)

    # reflected_status == "absent": delegate the operations-empty / fingerprint
    # / no-progress-retry math to decide_scope_reframe_contract_route() so
    # there is exactly one implementation of that logic.
    route_state = SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1(
        scope_delta_status=scope_delta_status or "",
        allowed_path_deltas=normalized_deltas,
        operations=list(operations),
        anchor_comment_url=anchor_comment_url,
        issue_body_sha256=issue_body_sha256,
        previous_empty_operations_fingerprints=list(previous_empty_operations_fingerprints or []),
    )
    route_result = decide_scope_reframe_contract_route(route_state)
    if route_result.route == ROUTE_ISSUE_EDITOR_REQUIRED:
        return ScopeReframeDecision(
            "full_rewrite_required",
            route_result.reason_code or REASON_CODE_APPROVED_SCOPE_REQUIRES_FULL_CONTRACT_REWRITE,
            route=ROUTE_ISSUE_EDITOR_REQUIRED,
            empty_operations_fingerprint=route_result.empty_operations_fingerprint,
            no_progress_retry_suppressed=route_result.no_progress_retry_suppressed,
            comment_action=route_result.comment_action,
        )
    return ScopeReframeDecision("patch", REASON_PATCH_OPERATIONS_DERIVED, route=ROUTE_CONTRACT_UPDATE)


# ---------------------------------------------------------------------------
# #2620: explicit trusted human_review_directive -> issue_editor_required
# handoff, independent of the STRUCTURED ANCHOR_SCOPE_REFRAME_V1 scope-
# reframe route above (decide_scope_reframe_contract_route() /
# classify_scope_reframe_disposition()). Routing priority (do not let this
# route shadow the existing ones):
#   1. existing section-bound CONTRACT_PATCH_PLAN_V1 safely derivable ->
#      existing contract_update route (classify_scope_reframe_disposition
#      "patch" disposition) -- unaffected.
#   2. existing approved scope-reframe full-rewrite (empty operations[] via
#      a STRUCTURED ANCHOR_SCOPE_REFRAME_V1 payload) -> existing
#      issue_editor_required, owned by decide_scope_reframe_contract_route()
#      above -- unaffected.
#   3. an explicit trusted human_review_directive whose derived
#      operations[] is empty (no safe section-bound patch representation
#      could be derived) and which is NOT governed by a structured scope
#      reframe -> issue_editor_required (THIS function; the new #2620
#      eligibility).
#   4. ambiguous / untrusted / invalid binding -> existing fail-closed,
#      writes=0 -- unaffected.
#
# Eligibility uses ONLY existing predicates the caller has independently
# re-derived from scope_signal_delta.classify_scope_delta_authority()
# against a FRESH SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 -- this function
# introduces no new classifier/heuristic of its own (#2620 Out of Scope);
# it only combines already-computed facts into a single routing decision,
# so there remains exactly one place (this SSOT) that decides the route.
# `run_refinement_preflight.py` may only call into this function -- it must
# never re-implement this eligibility combination as an ad-hoc branch of
# its own (that would duplicate the routing SSOT).
# ---------------------------------------------------------------------------

REASON_EXPLICIT_TRUSTED_HUMAN_DIRECTIVE_REQUIRES_ISSUE_EDITOR = (
    "explicit_trusted_human_directive_requires_issue_editor"
)

_AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE = "human_review_directive"
_DIRECTIVE_CONFIDENCE_EXPLICIT = "explicit"
_SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED = "contract_update_required"


@dataclass(frozen=True)
class HUMAN_REVIEW_DIRECTIVE_EDITOR_ROUTE_STATE_V1:
    """Input state for decide_human_review_directive_editor_route().

    Every field is a caller-supplied FACT the caller has already
    independently verified via an EXISTING predicate (#2620 In Scope: no
    new classifier/heuristic is introduced here). This function performs
    no re-derivation of trust, confidence, or binding of its own -- it only
    combines the supplied facts into a single eligibility decision.

    Fields:
        authority_category: `SCOPE_DELTA_AUTHORITY_EVIDENCE_V1`'s own
            `authority_category` from a FRESH
            `classify_scope_delta_authority()` re-classification (never
            inferred from the pre-built patch plan's mere shape).
        directive_confidence: the same fresh classification's
            `directive.confidence`.
        route_action: the same fresh classification's `route.action` --
            must be `contract_update_required` (the existing route action
            for an explicit, boundary-clear human_review_directive).
        with_human_context: True iff the anchor comment resolves to the
            trusted operator-selected `with_human_context` lane (existing
            `_resolve_scope_delta_source_kind() == "issue_comment"`
            predicate), never inferred from the directive text itself.
        anchor_binding_ok: True iff the evidence's own `comment_url` /
            `body_sha256` match THIS transaction's anchor URL / anchor
            body (fresh binding re-check, closing a TOCTOU window between
            evidence construction and this routing decision).
        same_target_ok: True iff the evidence's own `source_issue_number`
            matches the target Issue this transaction is operating on.
        operations_empty: True iff the freshly re-derived
            `contract_patch_plan.operations` is empty -- i.e. no safe
            section-bound patch representation exists.
        is_structured_scope_reframe: True iff a STRUCTURED
            `ANCHOR_SCOPE_REFRAME_V1` scope-reframe decision governs this
            same transaction -- when True, priority 2
            (`decide_scope_reframe_contract_route()`) owns the routing
            decision and this function must never shadow it.
        reviewer_feedback_url: the canonical anchor comment URL (never the
            raw `anchor_comment.snapshot` body text) to hand off to
            issue-editor when this route is selected.
    """

    authority_category: Optional[str]
    directive_confidence: Optional[str]
    route_action: Optional[str]
    with_human_context: bool
    anchor_binding_ok: bool
    same_target_ok: bool
    operations_empty: bool
    is_structured_scope_reframe: bool
    reviewer_feedback_url: Optional[str]


@dataclass(frozen=True)
class HumanReviewDirectiveEditorRouteResult:
    """Terminal result from decide_human_review_directive_editor_route().

    `route` is `None` when this transaction is not eligible for the #2620
    handoff (existing routes / fail-closed apply instead) -- callers MUST
    NOT construct an `issue_editor_required` route themselves when this
    result's `route` is `None`.
    """

    route: Optional[str]
    reason_code: Optional[str]
    reviewer_feedback_url: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "human_review_directive_editor_route_result/v1",
            "route": self.route,
            "reason_code": self.reason_code,
            "reviewer_feedback_url": (
                self.reviewer_feedback_url if self.route == ROUTE_ISSUE_EDITOR_REQUIRED else None
            ),
        }


def decide_human_review_directive_editor_route(
    state: HUMAN_REVIEW_DIRECTIVE_EDITOR_ROUTE_STATE_V1,
) -> HumanReviewDirectiveEditorRouteResult:
    """#2620 AC1/AC2: route an explicit trusted human_review_directive with
    no safe section-bound patch representation to `issue_editor_required`,
    writes=0 -- WITHOUT the caller (`preflight`/`contract_update`) ever
    executing the controlled transaction itself.

    Returns `route=None` (no opinion; existing behavior applies) unless
    EVERY one of the following existing-predicate facts holds:
      - `authority_category == "human_review_directive"`
      - `directive_confidence == "explicit"`
      - `route_action == "contract_update_required"`
      - `with_human_context` is True
      - `anchor_binding_ok` and `same_target_ok` are True
      - `operations_empty` is True (no safe section-bound patch exists)
      - `is_structured_scope_reframe` is False (priority 2 does not own
        this transaction)
      - `reviewer_feedback_url` is a non-empty string

    AC3/AC4: any single failing predicate returns `route=None` -- callers
    MUST treat that as "not eligible for this route" and fall through to
    the existing fail-closed / no_change behavior; this function never
    fails open.
    """
    eligible = (
        state.authority_category == _AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE
        and state.directive_confidence == _DIRECTIVE_CONFIDENCE_EXPLICIT
        and state.route_action == _SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED
        and state.with_human_context is True
        and state.anchor_binding_ok is True
        and state.same_target_ok is True
        and state.operations_empty is True
        and state.is_structured_scope_reframe is False
        and isinstance(state.reviewer_feedback_url, str)
        and bool(state.reviewer_feedback_url)
    )
    if not eligible:
        return HumanReviewDirectiveEditorRouteResult(route=None, reason_code=None, reviewer_feedback_url=None)
    return HumanReviewDirectiveEditorRouteResult(
        route=ROUTE_ISSUE_EDITOR_REQUIRED,
        reason_code=REASON_EXPLICIT_TRUSTED_HUMAN_DIRECTIVE_REQUIRES_ISSUE_EDITOR,
        reviewer_feedback_url=state.reviewer_feedback_url,
    )


# ---------------------------------------------------------------------------
# Schema validation (AC8 enforcement at runtime)
# ---------------------------------------------------------------------------


class RewriteRouterStateError(Exception):
    """
    Raised when a persisted router state file is present but corrupt, invalid,
    or schema-violating.

    This is deliberately distinct from a missing file (which returns None). A
    corrupt attempt-counter file must NOT be silently reset to 0, because that
    would let a process bypass the max_rewrite_attempts stop condition simply by
    truncating the state file. Callers must treat this as fail-closed.
    """


_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "schemas"
    / "loop_rewrite_router_state_v1.json"
)


def _load_schema() -> dict[str, Any]:
    """Load the LOOP_REWRITE_ROUTER_STATE_V1 JSON Schema from disk."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def validate_state_dict(data: Any) -> tuple[bool, str]:
    """
    Validate a state dict against loop_rewrite_router_state_v1.json.

    Unlike the previous required-fields-only check, this enforces the FULL
    schema: types, ranges (rewrite_attempt_count >= 0, max_rewrite_attempts >= 1),
    additionalProperties: false, sha256 format, and schema_version const.

    Returns (valid, error_message). Never raises for ordinary validation
    failures so callers can decide routing.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a declared dependency
        return False, "jsonschema is not available; cannot validate state"

    if not isinstance(data, dict):
        return False, "Input must be a JSON object"

    try:
        schema = _load_schema()
    except OSError as e:  # pragma: no cover - schema ships with the repo
        return False, f"Cannot load state schema: {e}"

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        return False, f"State schema validation failed: {e.message}"
    return True, ""


# ---------------------------------------------------------------------------
# Router state persistence helpers (AC7)
# ---------------------------------------------------------------------------


def load_rewrite_router_state(
    state_path: str,
    current_source_body_sha256: Optional[str] = None,
) -> Optional[LOOP_REWRITE_ROUTER_STATE_V1]:
    """
    Load LOOP_REWRITE_ROUTER_STATE_V1 from a JSON file.

    AC7: Supports replay-safe restoration — attempt count does NOT reset to 0
    across sessions / CI reruns.

    Return semantics (deliberately distinct so resets are never silent):
      - file missing            -> None (caller starts a fresh loop at attempt 0)
      - corrupt / invalid / schema-violating
                                -> raise RewriteRouterStateError (fail-closed;
                                   never silently reset the attempt counter)
      - source body changed     -> return a reset state with source_body_reset=True
                                   and rewrite_attempt_count=0; the reset fact is
                                   carried into the route result (AC7)
      - otherwise               -> restored state (replay_safe=True)
    """
    if not os.path.exists(state_path):
        return None

    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RewriteRouterStateError(
            f"Corrupt router state file {state_path}: {e}"
        ) from e
    except OSError as e:
        raise RewriteRouterStateError(
            f"Cannot read router state file {state_path}: {e}"
        ) from e

    valid, error_msg = validate_state_dict(data)
    if not valid:
        raise RewriteRouterStateError(
            f"Invalid router state file {state_path}: {error_msg}"
        )

    stored_source_sha = data.get("source_issue_body_sha256")

    # AC7: reset condition — human changed the source issue body. We do NOT
    # silently drop the state; we return a reset state that records the fact.
    if (
        current_source_body_sha256 is not None
        and stored_source_sha is not None
        and current_source_body_sha256 != stored_source_sha
    ):
        return LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=data["max_rewrite_attempts"],
            checker_exit_code=data["checker_exit_code"],
            checked_body_sha256=data["checked_body_sha256"],
            missing_sections=data.get("missing_sections", []),
            missing_contract_keys=data.get("missing_contract_keys", []),
            fix_category=data.get("fix_category", "unknown_contract_failure"),
            rewrite_history=[],
            occurrence_count=0,
            previous_checked_body_sha256=None,
            previous_missing_sections=[],
            previous_missing_contract_keys=[],
            source_issue_body_sha256=current_source_body_sha256,
            replay_safe=True,
            source_body_reset=True,
            # B4: AC9/AC10 reset to defaults on source body reset
            rewrite_request_fingerprint=data.get("rewrite_request_fingerprint"),
            previous_rewrite_request_fingerprints=list(data.get("previous_rewrite_request_fingerprints", [])),
            last_mutation_kind=data.get("last_mutation_kind", "semantic_rewrite"),
            budget_debit=data.get("budget_debit", 1),
        )

    return LOOP_REWRITE_ROUTER_STATE_V1(
        rewrite_attempt_count=data["rewrite_attempt_count"],
        max_rewrite_attempts=data["max_rewrite_attempts"],
        checker_exit_code=data["checker_exit_code"],
        checked_body_sha256=data["checked_body_sha256"],
        missing_sections=data.get("missing_sections", []),
        missing_contract_keys=data.get("missing_contract_keys", []),
        fix_category=data.get("fix_category", "unknown_contract_failure"),
        rewrite_history=list(data.get("rewrite_history", [])),
        occurrence_count=data.get("occurrence_count", 0),
        previous_checked_body_sha256=data.get("previous_checked_body_sha256"),
        previous_missing_sections=data.get("previous_missing_sections", []),
        previous_missing_contract_keys=data.get("previous_missing_contract_keys", []),
        source_issue_body_sha256=stored_source_sha,
        replay_safe=True,
        source_body_reset=False,
        # B4: AC9/AC10 now in JSON schema; read from JSON with safe defaults
        rewrite_request_fingerprint=data.get("rewrite_request_fingerprint"),
        previous_rewrite_request_fingerprints=list(data.get("previous_rewrite_request_fingerprints", [])),
        last_mutation_kind=data.get("last_mutation_kind", "semantic_rewrite"),
        budget_debit=data.get("budget_debit", 1),
    )


def save_rewrite_router_state(
    state: LOOP_REWRITE_ROUTER_STATE_V1,
    state_path: str,
) -> None:
    """
    Save LOOP_REWRITE_ROUTER_STATE_V1 to a JSON file atomically.

    AC7: Persists attempt count so it survives session restarts. The write is
    crash-safe: data is written to a temp file in the same directory, flushed and
    fsync'd, then os.replace()'d over the target. os.replace() is an atomic rename
    on success, so a crash mid-write can never leave a truncated / partial JSON
    file that load_rewrite_router_state would then treat as corrupt.

    NOTE: AC9/AC10 fields (rewrite_request_fingerprint, previous_rewrite_request_fingerprints,
    last_mutation_kind, budget_debit) are NOT persisted — the JSON schema has
    additionalProperties: false and these fields are in-memory only.
    """
    data = {
        "schema_version": SCHEMA_VERSION,
        "rewrite_attempt_count": state.rewrite_attempt_count,
        "max_rewrite_attempts": state.max_rewrite_attempts,
        "checker_exit_code": state.checker_exit_code,
        "checked_body_sha256": state.checked_body_sha256,
        "missing_sections": state.missing_sections,
        "missing_contract_keys": state.missing_contract_keys,
        "fix_category": state.fix_category,
        "rewrite_history": state.rewrite_history,
        "occurrence_count": state.occurrence_count,
        "previous_checked_body_sha256": state.previous_checked_body_sha256,
        "previous_missing_sections": state.previous_missing_sections,
        "previous_missing_contract_keys": state.previous_missing_contract_keys,
        "source_issue_body_sha256": state.source_issue_body_sha256,
        "replay_safe": True,
        "source_body_reset": state.source_body_reset,
        # B4: AC9/AC10 fields now in JSON schema
        "rewrite_request_fingerprint": state.rewrite_request_fingerprint,
        "previous_rewrite_request_fingerprints": list(state.previous_rewrite_request_fingerprints or []),
        "last_mutation_kind": state.last_mutation_kind,
        "budget_debit": state.budget_debit,
    }

    target_dir = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = f"{state_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _validate_cli_input(data: Any) -> tuple[bool, str]:
    """
    Validate CLI input against the full LOOP_REWRITE_ROUTER_STATE_V1 schema.

    This enforces types, ranges, additionalProperties: false, sha256 format, and
    schema_version const — not just required-field presence. Invalid input is
    rejected with an explicit reason (exit 2) rather than silently coerced.
    """
    return validate_state_dict(data)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. Reads LOOP_REWRITE_ROUTER_STATE_V1 JSON from stdin."""
    try:
        input_text = sys.stdin.read()
        input_data = json.loads(input_text)
    except json.JSONDecodeError as e:
        error = {"error": f"Invalid JSON input: {str(e)}", "route": None}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        sys.exit(2)

    valid, error_msg = _validate_cli_input(input_data)
    if not valid:
        error = {"error": error_msg, "route": None}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        sys.exit(2)

    try:
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=input_data["rewrite_attempt_count"],
            max_rewrite_attempts=input_data["max_rewrite_attempts"],
            checker_exit_code=input_data["checker_exit_code"],
            checked_body_sha256=input_data["checked_body_sha256"],
            missing_sections=input_data.get("missing_sections", []),
            missing_contract_keys=input_data.get("missing_contract_keys", []),
            fix_category=input_data.get("fix_category", "unknown_contract_failure"),
            rewrite_history=input_data.get("rewrite_history", []),
            occurrence_count=input_data.get("occurrence_count", 0),
            previous_checked_body_sha256=input_data.get("previous_checked_body_sha256"),
            previous_missing_sections=input_data.get("previous_missing_sections", []),
            previous_missing_contract_keys=input_data.get("previous_missing_contract_keys", []),
            source_issue_body_sha256=input_data.get("source_issue_body_sha256"),
            replay_safe=input_data.get("replay_safe", False),
            source_body_reset=input_data.get("source_body_reset", False),
            # B4: AC9/AC10 now in JSON schema; read from input with safe defaults
            rewrite_request_fingerprint=input_data.get("rewrite_request_fingerprint"),
            previous_rewrite_request_fingerprints=list(input_data.get("previous_rewrite_request_fingerprints", [])),
            last_mutation_kind=input_data.get("last_mutation_kind", "semantic_rewrite"),
            budget_debit=input_data.get("budget_debit", 1),
        )

        result = decide_rewrite_route(state)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(0)

    except Exception as e:
        error = {"error": f"Internal error: {str(e)}", "route": None}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        sys.exit(3)


if __name__ == "__main__":
    main()
