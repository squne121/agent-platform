"""Issue #2165: per-attempt/total deadline and retry-policy regression coverage.

The old `PER_ATTEMPT_DEADLINE_SECONDS = 90` could not fit even a single
legitimately long-running Verification Command (the `issue-refinement-loop`
skill's own full pytest suite, measured at 111.07s), and the closed
three-attempt retry matrix multiplied that fixed timeout without changing
the outcome (`3 * 90s = 270s` already exceeded the old
`TOTAL_DEADLINE_SECONDS = 240`). A subsequent fix (PER_ATTEMPT=300,
TOTAL=340) still undershot the layered budget ceiling `check_issue_contract`
(30s) + `contract_readiness_check` (250s) + `merge_readiness` (30s) = 310s >
300s add up to (OWNER 2026-08-15 REQUEST_CHANGES P1-1).

These tests pin the CURRENT design's internal consistency: the deadline
values are DERIVED (`run_root_review_pipeline.py`'s
`CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS` /
`CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS` /
`MERGE_READINESS_TIMEOUT_SECONDS`, which itself derives from
`contract_readiness_check.py`'s `CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS`,
which itself derives from `baseline_vc_preflight.py`'s per-VC-command cap)
rather than independently hand-picked, so a future edit to any one layer's
budget cannot silently reintroduce either arithmetic break without also
moving the values these tests assert on.
"""

from __future__ import annotations

import sys
from pathlib import Path

REFINEMENT_SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(REFINEMENT_SCRIPTS))
import reviewer_transport as transport  # noqa: E402
import run_root_review_pipeline as pipeline  # noqa: E402

CONTRACT_REVIEW_SCRIPTS = (
    Path(__file__).resolve().parents[4] / ".claude" / "skills" / "issue-contract-review" / "scripts"
)
if str(CONTRACT_REVIEW_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CONTRACT_REVIEW_SCRIPTS))
import contract_readiness_check  # noqa: E402
import baseline_vc_preflight  # noqa: E402

# Measured in Issue #2165's Background section: `1316 passed in 111.07s`.
MEASURED_FULL_SUITE_SECONDS = 111.07

# A valid V2 approve envelope, used below to make `retry_once_on_transport_
# failure()`'s second call succeed (mirrors
# `test_issue_reviewer_contract_static.py`'s `_APPROVE_STDOUT` fixture).
_APPROVE_STDOUT = transport.build_compact_v2(
    verdict="approve",
    summary="contract ready",
    blockers=0,
    reviewed_body_sha256="sha256:" + "a" * 64,
    attempt_id="fixture-attempt-2165",
    artifact_relative="2165/fixture-attempt-2165/attempt-001/compact_review_result_v2.json",
    artifact_sha256="sha256:" + "b" * 64,
).decode("utf-8")


# ---------------------------------------------------------------------------
# Layered-budget derivation chain (P1-1(c)): each layer's aggregate/wrapper
# timeout must be derived from -- and therefore must exceed -- the layer it
# wraps, and the chain must be internally consistent end to end.
# ---------------------------------------------------------------------------


def test_baseline_vc_preflight_aggregate_timeout_covers_two_near_cap_vcs():
    # A body with two VCs each just under the per-command cap must not be
    # able to exceed the aggregate wrapper timeout (the exact arithmetic
    # break the OWNER flagged: "two 150s VCs already exceed a 200s
    # aggregate").
    per_command_cap = baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS
    assert contract_readiness_check.BASELINE_VC_PREFLIGHT_AGGREGATE_TIMEOUT_SECONDS > per_command_cap * 2


def test_contract_readiness_check_wrapper_timeout_exceeds_its_own_internal_budget():
    internal_worst_case = (
        contract_readiness_check.VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS
        + contract_readiness_check.BASELINE_VC_PREFLIGHT_AGGREGATE_TIMEOUT_SECONDS
    )
    assert contract_readiness_check.CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS > internal_worst_case


def test_root_review_pipeline_derives_its_wrapper_timeout_from_contract_readiness_check():
    # No independent re-guess: the two constants must be the SAME value.
    assert (
        pipeline.CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS
        == contract_readiness_check.CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS
    )


def test_per_attempt_deadline_covers_measured_worst_case_vc_with_margin():
    # >= 35% margin above the measured full-suite VC duration.
    assert transport.PER_ATTEMPT_DEADLINE_SECONDS >= MEASURED_FULL_SUITE_SECONDS * 1.35


def test_per_attempt_deadline_module_fallback_covers_the_full_layered_ceiling():
    # The OWNER-flagged arithmetic break: PER_ATTEMPT_DEADLINE_SECONDS must
    # exceed check_issue_contract + contract_readiness_check + merge_readiness
    # sequential worst case (previously 30 + 250 + 30 = 310 > the old 300).
    layered_ceiling = (
        pipeline.CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS
        + pipeline.CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS
        + pipeline.MERGE_READINESS_TIMEOUT_SECONDS
    )
    assert transport.PER_ATTEMPT_DEADLINE_SECONDS > layered_ceiling


def test_total_deadline_does_not_starve_a_single_full_length_attempt():
    # `run_reviewer_transport()` bounds each attempt's wait() to
    # `min(per_attempt_deadline, total_deadline - elapsed)`; if the total
    # deadline were smaller than the per-attempt deadline, the very first
    # (and, for the deterministic backend, only) attempt would never get
    # its full budget.
    assert transport.TOTAL_DEADLINE_SECONDS >= transport.PER_ATTEMPT_DEADLINE_SECONDS


def test_min_retry_attempt_budget_fraction_blocks_a_second_full_length_deterministic_attempt():
    # For the deterministic backend's real production values, the
    # remaining margin after ONE full-length attempt
    # (TOTAL_DEADLINE_SECONDS - PER_ATTEMPT_DEADLINE_SECONDS = 40s) must be
    # SMALLER than the fraction-derived minimum retry budget
    # (`MIN_RETRY_ATTEMPT_BUDGET_FRACTION * PER_ATTEMPT_DEADLINE_SECONDS`),
    # so a second full-length attempt is never spawned -- matching
    # "effectively ONE full-length attempt" documented above.
    margin = transport.TOTAL_DEADLINE_SECONDS - transport.PER_ATTEMPT_DEADLINE_SECONDS
    min_retry_budget = transport.MIN_RETRY_ATTEMPT_BUDGET_FRACTION * transport.PER_ATTEMPT_DEADLINE_SECONDS
    assert margin < min_retry_budget


# ---------------------------------------------------------------------------
# Retry-policy allowlist (P1-3): the deterministic backend's retryable set
# is now a CLOSED allowlist of transport-layer-only reason codes, not
# "every reason_code except timeout".
# ---------------------------------------------------------------------------


def test_deterministic_backend_does_not_retry_timeout():
    assert (
        transport.retry_matrix(backend="deterministic", initial_session_ref=None, attempt=1, reason_code="timeout")
        is None
    )


def test_deterministic_backend_retryable_set_is_exactly_the_closed_allowlist():
    assert transport._DETERMINISTIC_RETRYABLE_REASON_CODES == frozenset(
        {"spawn_failure", "signal", "capture_failure"}
    )


def test_deterministic_backend_retries_only_allowlisted_transport_layer_failures():
    for reason_code in ("spawn_failure", "signal", "capture_failure"):
        assert (
            transport.retry_matrix(
                backend="deterministic", initial_session_ref=None, attempt=1, reason_code=reason_code
            )
            is not None
        )


def test_deterministic_backend_does_not_retry_deterministic_output_failures():
    # P1-3: retrying the SAME deterministic checker against the SAME pinned
    # body cannot change a `nonzero_exit`/`malformed_output`/`empty_output`/
    # `artifact_validation_failure` outcome -- these must NOT be retried for
    # the deterministic backend even though they remain retryable for
    # other backends below.
    for reason_code in ("nonzero_exit", "malformed_output", "empty_output", "artifact_validation_failure"):
        assert (
            transport.retry_matrix(
                backend="deterministic", initial_session_ref=None, attempt=1, reason_code=reason_code
            )
            is None
        )


def test_other_backends_still_retry_timeout_and_deterministic_output_failures_unchanged():
    # Issue #2161: the native Codex CLI executable backend ("codex") has
    # been retired from `reviewer_transport.py`, so this list no longer
    # includes it.
    for backend in ("claude", "fixture"):
        for reason_code in ("timeout", "nonzero_exit", "malformed_output"):
            assert (
                transport.retry_matrix(backend=backend, initial_session_ref=None, attempt=1, reason_code=reason_code)
                is not None
            )


def test_retry_matrix_respects_max_attempts_regardless_of_backend():
    assert (
        transport.retry_matrix(
            backend="deterministic",
            initial_session_ref=None,
            attempt=transport.MAX_ATTEMPTS,
            reason_code="spawn_failure",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Retry-budget reservation (P1-1 marge condition 8, scaled clock -- no real
# multi-minute sleeps).
# ---------------------------------------------------------------------------


def _run_spawn_failure_with_min_retry_fraction(tmp_path, fraction: float) -> dict:
    original_fraction = transport.MIN_RETRY_ATTEMPT_BUDGET_FRACTION
    transport.MIN_RETRY_ATTEMPT_BUDGET_FRACTION = fraction
    try:
        return transport.run_reviewer_transport(
            base_argv=[str(tmp_path / "missing-reviewer")],
            command_id="issue-reviewer.run",
            argv_template_id="issue-reviewer.run/v2",
            backend="fixture",
            issue_number=2165,
            repo="example-org/example-repo",
            reviewed_body_sha256="sha256:" + "e" * 64,
            artifact_root=tmp_path,
            invocation_id=f"retry-budget-regression-{fraction}",
            session_ref="same-session",
            per_attempt_deadline=1,
            total_deadline=10,
        )
    finally:
        transport.MIN_RETRY_ATTEMPT_BUDGET_FRACTION = original_fraction


def test_no_retry_attempt_is_spawned_once_remaining_budget_is_below_minimum(tmp_path):
    # A retryable failure (spawn_failure) on attempt 1, with a fraction
    # (20x per_attempt_deadline=1s -> 20s minimum retry budget) larger than
    # what could possibly remain from the 10s total_deadline, must not
    # spawn attempt 2 -- even though `retry_matrix()` alone would have
    # allowed a spawn_failure retry.
    result = _run_spawn_failure_with_min_retry_fraction(tmp_path, fraction=20)
    assert result["transport_status"] == "environment_failure"
    assert len(result["attempts"]) == 1


def test_retry_attempt_is_spawned_when_remaining_budget_is_above_minimum(tmp_path):
    # Same scenario, but a tiny fraction (well below what remains after
    # the near-instant first spawn failure): retries proceed normally up
    # to MAX_ATTEMPTS (control case proving the guard above is not just
    # always blocking every retry).
    result = _run_spawn_failure_with_min_retry_fraction(tmp_path, fraction=0.01)
    assert result["transport_status"] == "environment_failure"
    assert len(result["attempts"]) == transport.MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Merge condition #8 (PR #2177 OWNER 2026-08-15 REQUEST_CHANGES, fix_delta
# iteration 2): the retry-budget guard must apply uniformly regardless of
# backend, including the SEPARATE, OUTER `retry_once_on_transport_failure()`
# retry layer in `run_root_review_pipeline.py` (a different retry loop from
# `run_reviewer_transport()`'s own attempt loop, which the two tests above
# already cover for `backend="fixture"`).
# ---------------------------------------------------------------------------


def test_has_sufficient_retry_attempt_budget_is_backend_agnostic_by_construction():
    # The helper takes no `backend` argument at all -- it cannot special-case
    # any backend, so it necessarily applies uniformly to deterministic,
    # claude, and fixture callers alike.
    assert "backend" not in transport.has_sufficient_retry_attempt_budget.__code__.co_varnames[
        : transport.has_sufficient_retry_attempt_budget.__code__.co_argcount
    ]
    assert (
        transport.has_sufficient_retry_attempt_budget(
            elapsed_seconds=0.0, total_deadline_seconds=10.0, per_attempt_deadline_seconds=1.0
        )
        is True
    )
    assert (
        transport.has_sufficient_retry_attempt_budget(
            elapsed_seconds=9.95, total_deadline_seconds=10.0, per_attempt_deadline_seconds=1.0
        )
        is False
    )


def test_retry_once_on_transport_failure_skips_retry_when_budget_insufficient_for_non_deterministic_backend():
    # `invoke_child` here stands in for a non-deterministic (claude)
    # backend caller of `retry_once_on_transport_failure()`. Even though the
    # first call is a retryable transport failure (empty stdout), supplying
    # deadline state that leaves less than `MIN_RETRY_ATTEMPT_BUDGET_FRACTION
    # * per_attempt_deadline_seconds` of budget must skip the second call
    # entirely -- mirroring the `run_reviewer_transport()` attempt-loop guard
    # one layer up, for this SEPARATE outer retry loop.
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return ""

    result = pipeline.retry_once_on_transport_failure(
        invoke_child,
        issue_number=2165,
        elapsed_seconds=9.95,
        total_deadline_seconds=10.0,
        per_attempt_deadline_seconds=1.0,
    )
    assert calls["n"] == 1
    assert result["attempts"] == 1
    assert result["status"] == "reviewer_transport_failure"
    assert result["retry_skipped_reason"] == "insufficient_retry_budget"


def test_retry_once_on_transport_failure_still_retries_when_budget_sufficient_for_non_deterministic_backend():
    # Control case: with ample remaining budget, the second call still
    # happens -- the guard above is not just always blocking every retry.
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return "" if calls["n"] == 1 else _APPROVE_STDOUT

    result = pipeline.retry_once_on_transport_failure(
        invoke_child,
        issue_number=2165,
        elapsed_seconds=0.0,
        total_deadline_seconds=10.0,
        per_attempt_deadline_seconds=1.0,
    )
    assert calls["n"] == 2
    assert result["attempts"] == 2
    assert result["status"] == "ok"


def test_retry_once_on_transport_failure_defaults_to_unconditional_retry_when_deadline_state_omitted():
    # Backward compatibility: existing callers/tests that never pass
    # deadline state must keep the pre-#2165 unconditional
    # retry-exactly-once behavior unchanged.
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return "" if calls["n"] == 1 else _APPROVE_STDOUT

    result = pipeline.retry_once_on_transport_failure(invoke_child, issue_number=2165)
    assert calls["n"] == 2
    assert result["attempts"] == 2
    assert result["status"] == "ok"
