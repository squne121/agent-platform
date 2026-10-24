#!/usr/bin/env python3
"""Tests for join_review_results.py (Issue #2296 AC2; fix_delta iteration 6).

Covers the effective_verdict decision table:
- deterministic needs-fix always wins (semantic gate never evaluated)
- deterministic approve + semantic clear -> approve
- deterministic approve + severity blocker/high (open) -> needs-fix
  (+ rewrite_lane: semantic + semantic_rewrite_constraints, P0-4)
- deterministic approve + severity medium/low only -> approve + warning
- deterministic approve + severity blocker/high but a FULLY VALID
  owner_disposition (recorded_by=owner, status accepted/deferred, non-empty
  reason) -> approve + warning (P1-1)
- an incomplete/forged owner_disposition does NOT neutralize a blocker/high
  finding (P1-1)
- semantic_review_applicable=false (semantic_assessment=not_required) -> skip/approve
- unknown/invalid semantic_assessment or transport_status is treated as a
  transport failure, not silently approved (P0-3)
- assessment=clear with a blocker finding still routes to needs-fix (P0-3:
  verdict is computed from findings first, not from the assessment label)
- transport missing/stale/error x best_effort, first failure -> retry
  (P0-3/P1-5, not an immediate approve)
- transport missing/stale/error x best_effort, after one retry -> approve +
  durable SEMANTIC_REVIEW_UNAVAILABLE warning + semantic_review_unavailable: true
- transport missing/stale/error x required -> human_judgment_required
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import join_review_results as joiner  # noqa: E402


def test_deterministic_needs_fix_wins_regardless_of_semantic():
    result = joiner.join_review_results(
        deterministic_verdict="needs-fix",
        semantic_assessment="clear",
        transport_status="ok",
    )
    assert result["effective_verdict"] == "needs-fix"


def test_semantic_not_required_short_circuits_to_approve():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="not_required",
        transport_status="not_required",
    )
    assert result["effective_verdict"] == "approve"
    assert result["warnings"] == []


def test_semantic_clear_and_transport_ok_is_approve():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="clear",
        transport_status="ok",
    )
    assert result["effective_verdict"] == "approve"


def test_open_blocker_finding_routes_to_rewrite():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[{"severity": "blocker", "summary": "structural gap"}],
        source_artifact="/artifacts/1/abc/semantic_review_result.json",
        checked_body_sha256="a" * 64,
    )
    assert result["effective_verdict"] == "needs-fix"


def test_open_high_finding_routes_to_rewrite():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[{"severity": "high", "summary": "design gap"}],
    )
    assert result["effective_verdict"] == "needs-fix"


def test_needs_fix_from_semantic_finding_carries_rewrite_lane_and_constraints():
    """P0-4: a needs-fix verdict driven by an open semantic finding must
    carry rewrite_lane: semantic + semantic_rewrite_constraints so Step 4
    forwards it to issue-editor as-is."""
    finding = {"severity": "blocker", "summary": "structural gap"}
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[finding],
        source_artifact="/artifacts/1/abc/semantic_review_result.json",
        checked_body_sha256="a" * 64,
    )
    assert result["effective_verdict"] == "needs-fix"
    assert result["rewrite_lane"] == "semantic"
    constraints = result["semantic_rewrite_constraints"]
    assert constraints["schema_version"] == "SEMANTIC_REWRITE_CONSTRAINTS_V1"
    assert constraints["source_artifact"] == "/artifacts/1/abc/semantic_review_result.json"
    assert constraints["checked_body_sha256"] == "a" * 64
    assert constraints["findings"] == [finding]
    assert constraints["max_rewrite_attempts"] == 2
    assert constraints["no_progress_route"] == "human_judgment_required"


def test_assessment_clear_but_blocker_finding_present_still_routes_to_needs_fix():
    """P0-3: verdict is computed from findings FIRST, not from the
    assessment label -- a malformed producer output (assessment=clear but a
    blocker finding present) must not fail open into approve."""
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="clear",
        transport_status="ok",
        findings=[{"severity": "blocker", "summary": "inconsistent producer output"}],
    )
    assert result["effective_verdict"] == "needs-fix"


def test_unknown_semantic_assessment_value_is_treated_as_transport_failure():
    """P0-3: an unknown assessment enum value must not silently fall
    through to approve."""
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="unexpected_value",
        transport_status="ok",
        retry_already_attempted=True,
    )
    assert result["effective_verdict"] == "approve"
    assert result["semantic_review_unavailable"] is True
    assert any("invalid semantic_assessment" in w for w in result["warnings"])


def test_unknown_transport_status_value_is_treated_as_transport_failure():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="weird_status",
        findings=[{"severity": "low", "summary": "nit"}],
        retry_already_attempted=True,
    )
    assert result["effective_verdict"] == "approve"
    assert result["semantic_review_unavailable"] is True


def test_medium_low_only_findings_are_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {"severity": "medium", "summary": "nit"},
            {"severity": "low", "summary": "style"},
        ],
    )
    assert result["effective_verdict"] == "approve"
    assert result["warnings"]


def test_owner_accepted_blocker_finding_is_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {
                "severity": "blocker",
                "summary": "known tradeoff",
                "owner_disposition": {
                    "status": "accepted",
                    "recorded_by": "owner",
                    "reason": "explicitly accepted risk for v1",
                },
            }
        ],
    )
    assert result["effective_verdict"] == "approve"
    assert result["warnings"]


def test_owner_deferred_high_finding_is_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {
                "severity": "high",
                "summary": "later",
                "owner_disposition": {
                    "status": "deferred",
                    "recorded_by": "owner",
                    "reason": "follow-up Issue tracked separately",
                },
            }
        ],
    )
    assert result["effective_verdict"] == "approve"


def test_owner_disposition_without_recorded_by_owner_does_not_neutralize():
    """P1-1: a forged/incomplete owner_disposition (missing recorded_by)
    must NOT neutralize a blocker/high finding."""
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {
                "severity": "blocker",
                "summary": "not really accepted",
                "owner_disposition": {"status": "accepted", "reason": "self-accepted"},
            }
        ],
    )
    assert result["effective_verdict"] == "needs-fix"


def test_owner_disposition_without_reason_does_not_neutralize():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {
                "severity": "high",
                "summary": "missing reason",
                "owner_disposition": {"status": "accepted", "recorded_by": "owner"},
            }
        ],
    )
    assert result["effective_verdict"] == "needs-fix"


def test_transport_error_first_failure_best_effort_routes_to_retry():
    """P0-3/P1-5: a first transport failure under best_effort must route
    to retry, not an immediate approve."""
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="error",
        transport_policy="best_effort",
        retry_already_attempted=False,
    )
    assert result["effective_verdict"] == "retry"


def test_transport_missing_best_effort_after_retry_falls_back_to_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="missing",
        transport_policy="best_effort",
        retry_already_attempted=True,
    )
    assert result["effective_verdict"] == "approve"
    assert result["semantic_review_unavailable"] is True
    assert any(joiner.SEMANTIC_REVIEW_UNAVAILABLE_WARNING in w for w in result["warnings"])


def test_transport_stale_best_effort_first_failure_routes_to_retry():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="stale",
        transport_policy="best_effort",
    )
    assert result["effective_verdict"] == "retry"


def test_transport_error_required_escalates_to_human_judgment():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="error",
        transport_policy="required",
    )
    assert result["effective_verdict"] == "human_judgment_required"


def test_transport_missing_required_escalates_to_human_judgment():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="missing",
        transport_policy="required",
    )
    assert result["effective_verdict"] == "human_judgment_required"


def test_unknown_transport_policy_raises():
    import pytest

    with pytest.raises(ValueError):
        joiner.join_review_results(
            deterministic_verdict="approve",
            semantic_assessment="findings",
            transport_status="ok",
            transport_policy="strict",
        )


def test_unknown_finding_policy_raises():
    import pytest

    with pytest.raises(ValueError):
        joiner.join_review_results(
            deterministic_verdict="approve",
            semantic_assessment="findings",
            transport_status="ok",
            finding_policy="something_else",
        )


def test_default_policies_are_best_effort_and_route_high_open_to_rewrite():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="not_required",
    )
    assert result["transport_policy"] == "best_effort"
    assert result["finding_policy"] == "route_high_open_to_rewrite"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
