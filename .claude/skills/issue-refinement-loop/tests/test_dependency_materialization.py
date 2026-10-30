"""
Tests for dependency_materializer.py (Issue #2435).

Covers:
- AC3: explicit add/remove delta only, never a live-native full-set-replace
- AC4: independent postcondition readback re-verification (executor "ok" is
  not trusted blindly -- an observed mismatch is always reported as failure)
- AC5: body-only false-green fail-closed detector (#2424 incident shape)
- AC6: #2424-style regression fixture (desired={2422,2423,2432}, add-only)
- AC7(a): explicit-removal regression fixture (stale predecessor dropped)
- AC7(b): unrelated pre-existing native predecessor is preserved
- AC8: failure classification taxonomy
- AC9: the materializer is a plain, reusable, injectable function (no
  hidden global state -- a second, independent call site can reuse it)
- AC11: materialization result never claims implementation readiness
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import dependency_materializer as dm  # noqa: E402
from _trusted_repo import TRUSTED_REPO  # noqa: E402


# ---------------------------------------------------------------------------
# Producer: ISSUE_EXECUTION_DECISION_V1 -> desired predecessor set
# ---------------------------------------------------------------------------


def test_dependency_materialization_derive_desired_predecessors_from_depends_on_relation():
    """GIVEN an ISSUE_EXECUTION_DECISION_V1 with a depends_on relation
    WHEN deriving the desired predecessor set for the source issue
    THEN the target of that relation is included."""
    decision = {
        "relations": [
            {"source_issue_number": 10, "target_issue_number": 20, "relation_type": "depends_on"},
            {"source_issue_number": 10, "target_issue_number": 30, "relation_type": "coordinates"},
        ],
        "execution": {"predecessors": []},
    }
    assert dm.derive_desired_predecessors(decision, 10) == [20]


def test_dependency_materialization_derive_desired_predecessors_unions_predecessors_field():
    """GIVEN both a depends_on relation and an execution.predecessors entry
    WHEN deriving the desired set
    THEN both are unioned and deduplicated."""
    decision = {
        "relations": [{"source_issue_number": 1, "target_issue_number": 2, "relation_type": "depends_on"}],
        "execution": {"predecessors": [2, 3]},
    }
    assert dm.derive_desired_predecessors(decision, 1) == [2, 3]


def test_dependency_materialization_derive_desired_predecessors_ignores_other_sources_and_bools():
    """GIVEN relations for a different source issue and a boolean masquerading
    as an int predecessor
    WHEN deriving the desired set
    THEN neither is included (booleans are a subclass of int in Python)."""
    decision = {
        "relations": [{"source_issue_number": 99, "target_issue_number": 20, "relation_type": "depends_on"}],
        "execution": {"predecessors": [True]},
    }
    assert dm.derive_desired_predecessors(decision, 1) == []


def test_dependency_materialization_derive_desired_predecessors_handles_missing_input():
    """GIVEN a missing/malformed decision
    WHEN deriving the desired set
    THEN it fails closed to an empty list rather than raising."""
    assert dm.derive_desired_predecessors(None, 1) == []
    assert dm.derive_desired_predecessors({}, 1) == []


def test_dependency_materialization_derive_stale_predecessors_is_explicit_decision_delta():
    """GIVEN two confirmed decision snapshots (previous vs current)
    WHEN a predecessor present in the previous decision is absent from the
    current one
    THEN it becomes an explicit stale-predecessor-to-remove entry -- this is
    the only source of a removal instruction this module ever produces
    (AC3/AC7a)."""
    previous = {
        "relations": [
            {"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2423, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2432, "relation_type": "depends_on"},
        ]
    }
    current = {
        "relations": [
            {"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2432, "relation_type": "depends_on"},
        ]
    }
    assert dm.derive_stale_predecessors(previous, current, 2424) == [2423]


def test_dependency_materialization_derive_stale_predecessors_no_previous_decision_yields_empty():
    """GIVEN no previous confirmed decision (first materialization)
    WHEN deriving the stale set
    THEN it is empty -- nothing has ever been confirmed before, so nothing
    can be explicitly stale yet."""
    current = {"relations": [{"source_issue_number": 1, "target_issue_number": 2, "relation_type": "depends_on"}]}
    assert dm.derive_stale_predecessors(None, current, 1) == []


# ---------------------------------------------------------------------------
# Delta / postcondition math
# ---------------------------------------------------------------------------


def test_dependency_materialization_compute_add_and_remove_targets_add_only():
    """AC6: #2424-style fixture -- nothing live yet, three desired
    predecessors -- should be a pure additive delta."""
    add, remove = dm.compute_add_and_remove_targets([2422, 2423, 2432], [], [])
    assert add == [2422, 2423, 2432]
    assert remove == []


def test_dependency_materialization_compute_add_and_remove_targets_never_removes_unrelated_blocker():
    """AC7(b): an explicit-remove list must never touch a pre-existing,
    unrelated native predecessor (fixture #99) simply because it is absent
    from the desired set -- only explicit removal instructions can drop
    anything."""
    add, remove = dm.compute_add_and_remove_targets(
        desired_predecessors=[42],
        stale_predecessors_to_remove=[],
        live_predecessors_before=[99],
    )
    assert remove == []
    assert add == [42]


def test_dependency_materialization_compute_add_and_remove_targets_explicit_removal():
    """AC7(a): explicit stale predecessor is removed when live and desired no
    longer includes it."""
    add, remove = dm.compute_add_and_remove_targets(
        desired_predecessors=[2422, 2432],
        stale_predecessors_to_remove=[2423],
        live_predecessors_before=[2422, 2423, 2432, 99],
    )
    assert add == []
    assert remove == [2423]


def test_dependency_materialization_compute_add_and_remove_targets_remove_bounded_to_live_set():
    """A stale-predecessor instruction for something not actually live is a
    no-op, not an error -- remove is bounded to the live set."""
    add, remove = dm.compute_add_and_remove_targets([], [123], [])
    assert add == []
    assert remove == []


def test_dependency_materialization_compute_expected_predecessors_after_preserves_unrelated_blocker():
    """AC3: postcondition formula (live_before - remove) | add, independent
    of what was 'desired' -- an untouched unrelated predecessor survives."""
    expected = dm.compute_expected_predecessors_after(
        live_predecessors_before=[99, 2422, 2423, 2432],
        add_targets=[],
        remove_targets=[2423],
    )
    assert expected == [99, 2422, 2432]


# ---------------------------------------------------------------------------
# Failure classification (AC8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_code", "expected_class"),
    [
        ("relationship_pre_readback_failed", "auth-or-environment-failure"),
        ("post_relationship_readback_failed", "auth-or-environment-failure"),
        ("expected_before_drift_detected", "readback-mismatch"),
        ("final_native_relationship_readback_drift", "readback-mismatch"),
        ("concurrent_content_drift_after_relationship_mutation", "readback-mismatch"),
        ("graph_invariant_violation", "semantic-human-judgment-required"),
        ("issue_relationship_update_failed", "controlled-executor-failure"),
        ("issue_relationship_update_child_receipt_lost", "controlled-executor-failure"),
        ("some_unknown_future_code", "controlled-executor-failure"),
    ],
)
def test_dependency_materialization_classify_failure_by_error_code(error_code: str, expected_class: str):
    assert dm.classify_materialization_failure(error_code=error_code) == expected_class


def test_dependency_materialization_classify_failure_human_judgment_readiness_status():
    assert (
        dm.classify_materialization_failure(readiness_status="human_judgment") == "semantic-human-judgment-required"
    )
    assert (
        dm.classify_materialization_failure(readiness_status="input_or_runtime_error")
        == "semantic-human-judgment-required"
    )


def test_dependency_materialization_classify_failure_none_when_no_signal():
    assert dm.classify_materialization_failure() is None


# ---------------------------------------------------------------------------
# Body-only false-green detector (AC5)
# ---------------------------------------------------------------------------


def test_dependency_materialization_detect_body_only_false_green_fails_closed():
    """AC5: the #2424 incident shape -- native capability was available, the
    body declares predecessors under '## Blocked By', but native
    materialization was never even attempted."""
    body = "## Blocked By\n\n- #2422\n- #2423\n- #2432\n\n## Other Section\n"
    triggered, reason = dm.detect_body_only_false_green(
        body, native_relationship_attempted=False, capability_available=True
    )
    assert triggered is True
    assert reason == "body_only_predecessor_mutation_without_native_materialization"


def test_dependency_materialization_detect_body_only_false_green_allows_when_attempted():
    body = "## Blocked By\n\n- #2422\n"
    triggered, reason = dm.detect_body_only_false_green(
        body, native_relationship_attempted=True, capability_available=True
    )
    assert triggered is False
    assert reason is None


def test_dependency_materialization_detect_body_only_false_green_allows_when_capability_unavailable():
    """When native capability was genuinely unavailable (SKIP-eligible
    environment failure, not a body-only shortcut), this is not the #2424
    false-green shape."""
    body = "## Blocked By\n\n- #2422\n"
    triggered, reason = dm.detect_body_only_false_green(
        body, native_relationship_attempted=False, capability_available=False
    )
    assert triggered is False
    assert reason is None


def test_dependency_materialization_extract_body_declared_predecessors_stops_at_next_heading():
    body = "## Blocked By\n- #10\n- #20\n\n## Notes\nSee #30 for context.\n"
    assert dm.extract_body_declared_predecessors(body) == [10, 20]


def test_dependency_materialization_extract_body_declared_predecessors_empty_without_heading():
    assert dm.extract_body_declared_predecessors("no relevant section here") == []


# ---------------------------------------------------------------------------
# materialize_dependencies orchestration (injectable collaborators; no live
# GitHub call -- consistent with edit_issue_txn.py's own test convention)
# ---------------------------------------------------------------------------


def _fake_edit_txn_invoke_ok(payload: dict[str, Any], *, issue_number: int) -> dict[str, Any]:
    native = payload["native_relationships"]
    before = native["expected_before"]["blocked_by"]
    add = native["add_blocked_by"]
    remove = native["remove_blocked_by"]
    after = sorted((set(before) - set(remove)) | set(add))
    return {
        "schema": "ISSUE_EDIT_TXN_RESULT_V1",
        "status": "ok",
        "errors": [],
        "native_relationships": {
            "attempted": True,
            "status": "applied",
            "before": {"blocked_by": before},
            "after": {"blocked_by": after},
            "errors": [],
        },
    }


def _fake_issue_content(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
    return {"title": "t", "body": "body text", "updatedAt": "2026-08-30T00:00:00Z"}, ""


def test_dependency_materialization_materialize_dependencies_2424_regression_add_only():
    """AC6: #2424-type fixture -- desired blockers {2422,2423,2432}, nothing
    live yet -- the mutation instruction is add-only and the independently
    recomputed postcondition matches the observed readback exactly."""

    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [], "blocking": []}, ""

    result = dm.materialize_dependencies(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422, 2423, 2432],
        stale_predecessors_to_remove=[],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=_fake_edit_txn_invoke_ok,
    )
    assert result["status"] == "ok"
    assert result["native_relationship_materialized"] is True
    assert result["observed_predecessors_after"] == [2422, 2423, 2432]
    assert result["expected_predecessors_after"] == [2422, 2423, 2432]


def test_dependency_materialization_materialize_dependencies_reframe_removes_stale_and_preserves_unrelated():
    """AC7(a)+(b) combined in one live-shaped scenario: reframing from
    {2422,2423,2432} to {2422,2432} explicitly removes #2423 via an explicit
    remove instruction, while an unrelated pre-existing native predecessor
    (#99, never confirmed by this pipeline) survives untouched."""

    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [99, 2422, 2423, 2432], "blocking": []}, ""

    result = dm.materialize_dependencies(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422, 2432],
        stale_predecessors_to_remove=[2423],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=_fake_edit_txn_invoke_ok,
    )
    assert result["status"] == "ok"
    assert result["observed_predecessors_after"] == [99, 2422, 2432]
    assert 2423 not in result["observed_predecessors_after"]
    assert 99 in result["observed_predecessors_after"]


def test_dependency_materialization_materialize_dependencies_readback_mismatch_never_reported_ok():
    """AC4: even when the underlying executor self-reports status: ok, this
    module independently recomputes the expected postcondition and reports
    failure on any mismatch -- it never trusts the executor's self-report
    alone."""

    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [], "blocking": []}, ""

    def fake_invoke_drifted(payload: dict[str, Any], *, issue_number: int) -> dict[str, Any]:
        return {
            "status": "ok",
            "errors": [],
            "native_relationships": {
                "attempted": True,
                "status": "applied",
                "before": {"blocked_by": []},
                "after": {"blocked_by": [999999]},
                "errors": [],
            },
        }

    result = dm.materialize_dependencies(
        target_issue_number=1,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422],
        stale_predecessors_to_remove=[],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=fake_invoke_drifted,
    )
    assert result["status"] == "failed"
    assert result["failure_class"] == "readback-mismatch"
    assert result["native_relationship_materialized"] is False


def test_dependency_materialization_materialize_dependencies_capability_unavailable_fails_closed():
    """AC8: gh binary missing is classified as native-capability-unavailable
    and never silently treated as success."""
    result = dm.materialize_dependencies(
        target_issue_number=1,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422],
        capability_preflight=lambda: (False, "gh_binary_not_found"),
    )
    assert result["status"] == "blocked"
    assert result["failure_class"] == "native-capability-unavailable"
    assert result["native_relationship_materialized"] is False


def test_dependency_materialization_materialize_dependencies_auth_unreachable_is_environment_failure():
    """AC8: gh present but unauthenticated/unreachable is a routine
    environment failure, distinct from capability-unavailable, and never
    converted into an unnecessary human-judgment escalation."""
    result = dm.materialize_dependencies(
        target_issue_number=1,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422],
        capability_preflight=lambda: (False, "gh_auth_status_unreachable"),
    )
    assert result["failure_class"] == "auth-or-environment-failure"


def test_dependency_materialization_materialize_dependencies_no_op_when_already_live():
    """When the desired set already matches live state exactly, no mutation
    is attempted at all (AC3 -- only an explicit delta ever triggers a
    write)."""

    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [2422], "blocking": []}, ""

    def fail_if_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("edit_issue_txn must not be invoked when there is no delta")

    result = dm.materialize_dependencies(
        target_issue_number=1,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=fail_if_called,
    )
    assert result["status"] == "ok"
    assert result["edit_txn_status"] == "no_op_not_attempted"


def test_dependency_materialization_materialize_dependencies_reusable_across_independent_call_sites():
    """AC9: the same function, unmodified, can be called twice in a row for
    two different target issues without any shared/leaking state -- so a
    second lane (e.g. #2406's confirmed-hard-predecessor route) can reuse it
    directly."""

    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [], "blocking": []}, ""

    first = dm.materialize_dependencies(
        target_issue_number=100,
        repo=TRUSTED_REPO,
        desired_predecessors=[1],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=_fake_edit_txn_invoke_ok,
    )
    second = dm.materialize_dependencies(
        target_issue_number=200,
        repo=TRUSTED_REPO,
        desired_predecessors=[2],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=_fake_edit_txn_invoke_ok,
    )
    assert first["target_issue_number"] == 100
    assert second["target_issue_number"] == 200
    assert first["observed_predecessors_after"] == [1]
    assert second["observed_predecessors_after"] == [2]


# ---------------------------------------------------------------------------
# AC11: result never conflates materialization success with implementation
# readiness (predecessor-open/closed is #265's responsibility, not this
# module's)
# ---------------------------------------------------------------------------


def test_dependency_materialization_result_has_no_implementation_readiness_field():
    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [], "blocking": []}, ""

    result = dm.materialize_dependencies(
        target_issue_number=1,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=_fake_edit_txn_invoke_ok,
    )
    forbidden_field_fragments = ("implementation_ready", "readiness_gate", "open_predecessor")
    for key in result:
        for fragment in forbidden_field_fragments:
            assert fragment not in key, f"result field {key!r} conflates materialization with readiness"
    assert set(result) == {
        "schema",
        "status",
        "native_relationship_materialized",
        "failure_class",
        "target_issue_number",
        "repo",
        "desired_predecessors",
        "stale_predecessors_to_remove",
        "live_predecessors_before",
        "expected_predecessors_after",
        "observed_predecessors_after",
        "edit_txn_status",
        "edit_txn_result_ref",
        "errors",
    }


# ---------------------------------------------------------------------------
# AC10: this module never re-declares edit_issue_txn.py's native
# relationship schema key literals as duplicate hardcoded strings -- it
# resolves them from edit_issue_txn.py's own constant.
# ---------------------------------------------------------------------------


def test_dependency_materialization_resolves_native_predecessor_keys_from_edit_issue_txn():
    edit_txn = dm._load_edit_issue_txn()
    add_key, remove_key = dm._resolve_native_predecessor_delta_keys(edit_txn)
    assert add_key == "add_blocked_by"
    assert remove_key == "remove_blocked_by"
    assert {add_key, remove_key}.issubset(edit_txn.NATIVE_RELATIONSHIPS_KEYS)


# ---------------------------------------------------------------------------
# P1 (OWNER review on PR #2447): this module must never re-invoke
# contract_readiness_check.py itself -- it only forwards a caller-supplied
# readiness_result, or falls back to a no-op "go" default computed without
# any subprocess call.
# ---------------------------------------------------------------------------


def test_dependency_materialization_never_shells_out_for_readiness():
    """The old `_run_readiness_check` subprocess helper no longer exists at
    all -- there is no internal seam left that could re-invoke
    contract_readiness_check.py."""
    assert not hasattr(dm, "_run_readiness_check")
    assert not hasattr(dm, "READINESS_SCRIPT")


def test_dependency_materialization_forwards_caller_supplied_readiness_result():
    """A caller-supplied readiness_result is forwarded verbatim into the
    edit_issue_txn.py payload rather than recomputed."""
    captured: dict[str, Any] = {}

    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [], "blocking": []}, ""

    def capture_invoke(payload: dict[str, Any], *, issue_number: int) -> dict[str, Any]:
        captured["readiness_result"] = payload["readiness_forwarding_payload"]["readiness_result"]
        return _fake_edit_txn_invoke_ok(payload, issue_number=issue_number)

    caller_readiness = {
        "status": "needs_fix",
        "body_sha256": "sha256:deadbeef",
        "source_checks": ["caller_provided"],
        "errors": [],
        "readiness_result_ref": "artifacts/caller_provided.json",
    }

    dm.materialize_dependencies(
        target_issue_number=1,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422],
        readiness_result=caller_readiness,
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=capture_invoke,
    )
    assert captured["readiness_result"] == caller_readiness


def test_dependency_materialization_default_readiness_result_does_not_invoke_subprocess(monkeypatch):
    """When the caller omits readiness_result, no subprocess of any kind is
    started by this module to compute one."""

    def fail_if_called(*args: Any, **kwargs: Any):
        raise AssertionError("dependency_materializer.py must never shell out for its own readiness check")

    monkeypatch.setattr(dm.subprocess, "run", fail_if_called)

    def fake_live(issue_number: int, repo: str) -> tuple[dict[str, Any], str]:
        return {"parent": None, "blocked_by": [], "blocking": []}, ""

    result = dm.materialize_dependencies(
        target_issue_number=1,
        repo=TRUSTED_REPO,
        desired_predecessors=[2422],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=fake_live,
        fetch_issue_content=_fake_issue_content,
        invoke_edit_issue_txn=_fake_edit_txn_invoke_ok,
    )
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# P0 (OWNER review on PR #2447): evaluate_termination_dependency_gate() --
# the mandatory Step 5 termination choke point. These tests exercise the
# gate end-to-end (not just the standalone detect_body_only_false_green
# unit tests above), per the owner's guidance to favor a small number of
# high-value integration-shaped tests.
# ---------------------------------------------------------------------------


def test_termination_gate_no_hard_dependency_confirmed_is_a_noop():
    """A cycle that confirmed no hard dependency at all must not be forced
    to have one -- the gate is a no-op and never calls materialize()."""

    def fail_if_called(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("materialize must not be called when no hard dependency was confirmed")

    result = dm.evaluate_termination_dependency_gate(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        current_execution_decision=None,
        materialize=fail_if_called,
    )
    assert result["decision"] == "proceed"
    assert result["materialization_result"] is None
    assert result["body_only_false_green"] is False


def test_termination_gate_2424_regression_fixture_proceeds():
    """AC6-shaped fixture through the gate: desired={2422,2423,2432},
    materialization succeeds, and the gate allows the approved termination
    to proceed."""
    decision = {
        "relations": [
            {"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2423, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2432, "relation_type": "depends_on"},
        ]
    }

    def fake_materialize(**kwargs: Any) -> dict[str, Any]:
        assert sorted(kwargs["desired_predecessors"]) == [2422, 2423, 2432]
        return {
            "schema": dm.RESULT_SCHEMA,
            "status": "ok",
            "native_relationship_materialized": True,
            "failure_class": None,
            "edit_txn_status": "ok",
            "observed_predecessors_after": [2422, 2423, 2432],
        }

    result = dm.evaluate_termination_dependency_gate(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        current_execution_decision=decision,
        current_body="## Outcome\n\nSome outcome text.\n",
        materialize=fake_materialize,
    )
    assert result["decision"] == "proceed"
    assert result["materialization_result"]["observed_predecessors_after"] == [2422, 2423, 2432]


def test_termination_gate_explicit_remove_with_unrelated_blocker_preservation_proceeds():
    """AC7(a)+(b) through the gate: reframing from {2422,2423,2432} to
    {2422,2432} explicitly removes #2423 via the previous/current decision
    delta, while an unrelated pre-existing blocker (#99) is preserved by the
    underlying materializer -- the gate still proceeds."""
    previous_decision = {
        "relations": [
            {"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2423, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2432, "relation_type": "depends_on"},
        ]
    }
    current_decision = {
        "relations": [
            {"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"},
            {"source_issue_number": 2424, "target_issue_number": 2432, "relation_type": "depends_on"},
        ]
    }

    def fake_materialize(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["stale_predecessors_to_remove"] == [2423]
        return {
            "schema": dm.RESULT_SCHEMA,
            "status": "ok",
            "native_relationship_materialized": True,
            "failure_class": None,
            "edit_txn_status": "ok",
            "observed_predecessors_after": [99, 2422, 2432],
        }

    result = dm.evaluate_termination_dependency_gate(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        current_execution_decision=current_decision,
        previous_execution_decision=previous_decision,
        current_body="## Outcome\n\nSome outcome text.\n",
        materialize=fake_materialize,
    )
    assert result["decision"] == "proceed"
    assert 99 in result["materialization_result"]["observed_predecessors_after"]
    assert 2423 not in result["materialization_result"]["observed_predecessors_after"]


def test_termination_gate_body_only_false_green_blocks_approved_termination_end_to_end():
    """The #2424 incident shape wired end-to-end: the body already declares
    predecessors under '## Blocked By', native capability was available,
    but native materialization was never attempted (e.g. a transient live
    readback failure before any write was attempted) -- the gate must
    block the approved termination, not just flag it in a standalone
    detector unit test."""
    decision = {
        "relations": [
            {"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"},
        ]
    }

    def fake_materialize(**kwargs: Any) -> dict[str, Any]:
        # capability preflight succeeded (not native-capability-unavailable)
        # but the live readback failed before any write was attempted.
        return {
            "schema": dm.RESULT_SCHEMA,
            "status": "blocked",
            "native_relationship_materialized": False,
            "failure_class": "auth-or-environment-failure",
            "edit_txn_status": None,
        }

    body = "## Blocked By\n\n- #2422\n\n## Outcome\n\nSome outcome text.\n"
    result = dm.evaluate_termination_dependency_gate(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        current_execution_decision=decision,
        current_body=body,
        materialize=fake_materialize,
    )
    assert result["decision"] == "block_persistent"
    assert result["body_only_false_green"] is True
    assert result["reason"] == "body_only_false_green_detected"


def test_termination_gate_retryable_failure_without_body_only_blocks_retryable():
    """A transient environment failure with no body-only shortcut in play
    (the body does not declare any predecessors yet) must be retryable, not
    escalated to a human."""
    decision = {
        "relations": [{"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"}]
    }

    def fake_materialize(**kwargs: Any) -> dict[str, Any]:
        return {
            "schema": dm.RESULT_SCHEMA,
            "status": "blocked",
            "native_relationship_materialized": False,
            "failure_class": "auth-or-environment-failure",
            "edit_txn_status": None,
        }

    result = dm.evaluate_termination_dependency_gate(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        current_execution_decision=decision,
        current_body="## Outcome\n\nNo blocked-by section here.\n",
        materialize=fake_materialize,
    )
    assert result["decision"] == "block_retryable"


@pytest.mark.parametrize(
    ("failure_class", "edit_txn_status"),
    [
        ("readback-mismatch", "ok"),
        ("semantic-human-judgment-required", "human_judgment"),
        ("native-capability-unavailable", None),
    ],
)
def test_termination_gate_persistent_failure_classes_block_persistent(failure_class: str, edit_txn_status: str | None):
    decision = {
        "relations": [{"source_issue_number": 2424, "target_issue_number": 2422, "relation_type": "depends_on"}]
    }

    def fake_materialize(**kwargs: Any) -> dict[str, Any]:
        return {
            "schema": dm.RESULT_SCHEMA,
            "status": "failed",
            "native_relationship_materialized": False,
            "failure_class": failure_class,
            "edit_txn_status": edit_txn_status,
        }

    result = dm.evaluate_termination_dependency_gate(
        target_issue_number=2424,
        repo=TRUSTED_REPO,
        current_execution_decision=decision,
        current_body="## Outcome\n\nNo blocked-by section here.\n",
        materialize=fake_materialize,
    )
    assert result["decision"] == "block_persistent"
