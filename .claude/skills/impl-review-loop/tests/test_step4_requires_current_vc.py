"""Issue #88 AC1/AC4/AC7: pr-reviewer must not be invoked without a
current-head, non-blocking VC_ADJUDICATION_RESULT_V1.

These tests exercise `evaluate_step4_vc_gate` added to
`adjudicate_vc_result.py`, using a lightweight in-process loop simulation
(no persistent ledger, no new schema, no new hook -- Issue #88 Required
Design #9 / Out of Scope). Same-loop reuse across separate LOOP_STATE
lookups (`step4_gate_from_loop_state` / `step4_persist_vc_adjudication`,
Issue #88 fix_delta Blocker 2) is covered separately in
`test_adjudicate_vc_result_non_regression_gate.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "adjudicate_vc_result.py"
)


spec = importlib.util.spec_from_file_location("adjudicate_vc_result", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


HEAD = "a" * 40
BODY_SHA = "sha256:" + "b" * 64
COMMAND_HASHES = ["sha256:" + f"{i:x}" * 64 for i in (1, 2)]


def _valid_pass_adjudication(
    *,
    head_sha: str = HEAD,
    contract_body_sha256: str = BODY_SHA,
    command_hashes: list[str] | None = None,
) -> dict[str, Any]:
    hashes = command_hashes if command_hashes is not None else COMMAND_HASHES
    return {
        "schema": mod.SCHEMA_NAME,
        "schema_version": mod.SCHEMA_VERSION,
        "overall_status": "pass",
        "blocking": False,
        "rerun_required": False,
        "per_ac": [
            {
                "ac": f"AC{index + 1}",
                "status": "pass",
                "blocking": False,
                "command_hash": command_hash,
                "failure_keys": [],
                "reason_code": "expected_pass_still_passes",
                "summary": "ok",
            }
            for index, command_hash in enumerate(hashes)
        ],
        "evidence_refs": [],
        "source_integrity": {
            "head_sha": head_sha,
            "contract_body_sha256": contract_body_sha256,
            "evidence_fresh": True,
            "evidence_complete": True,
        },
        "errors": [],
        "artifact_ref": None,
        "artifact_digest": None,
        "stdout_truncated": False,
        "omitted_fields": [],
    }


def _blocking_adjudication(*, overall_status: str) -> dict[str, Any]:
    result = _valid_pass_adjudication()
    result["overall_status"] = overall_status
    result["blocking"] = True
    result["per_ac"][0]["status"] = overall_status
    result["per_ac"][0]["blocking"] = True
    return result


def _simulate_step4_loop(
    *,
    adjudication_result: Any,
    expected_head_sha: str = HEAD,
    expected_contract_body_sha256: str = BODY_SHA,
    expected_command_hashes: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """GIVEN an adjudication result, WHEN Step 4 evaluates the gate,
    THEN return (gate_decision, pr_reviewer_invocation_count)."""
    hashes = expected_command_hashes if expected_command_hashes is not None else COMMAND_HASHES
    pr_reviewer_calls = {"count": 0}

    def _spawn_pr_reviewer() -> None:
        pr_reviewer_calls["count"] += 1

    decision = mod.evaluate_step4_vc_gate(
        adjudication_result,
        expected_head_sha=expected_head_sha,
        expected_contract_body_sha256=expected_contract_body_sha256,
        expected_command_hashes=hashes,
    )
    if decision["invoke_pr_reviewer"]:
        _spawn_pr_reviewer()
    return decision, pr_reviewer_calls["count"]


# --- AC1: no current-head nonblocking adjudication -> pr-reviewer not invoked ---


def test_ac1_missing_adjudication_blocks_pr_reviewer_invocation():
    # GIVEN no VC_ADJUDICATION_RESULT_V1 was produced for the linked Issue's VCs
    # WHEN Step 4 evaluates the gate
    # THEN pr-reviewer must not be invoked
    decision, calls = _simulate_step4_loop(adjudication_result=None)

    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "adjudication_missing_or_malformed"
    assert calls == 0


def test_ac1_valid_matching_adjudication_allows_pr_reviewer_invocation():
    # GIVEN a fresh, non-blocking VC_ADJUDICATION_RESULT_V1 for the current head
    # WHEN Step 4 evaluates the gate
    # THEN pr-reviewer may be invoked exactly once
    adjudication = _valid_pass_adjudication()

    decision, calls = _simulate_step4_loop(adjudication_result=adjudication)

    assert decision["invoke_pr_reviewer"] is True
    assert decision["reason_code"] is None
    assert calls == 1


# --- AC4: FAIL / SKIP / fallback / missing / malformed block reviewer spawn ---


def test_ac4_malformed_adjudication_blocks_pr_reviewer():
    # GIVEN a malformed payload (missing the VC_ADJUDICATION_RESULT_V1 schema tag)
    decision, calls = _simulate_step4_loop(adjudication_result={"not": "a real result"})

    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "adjudication_missing_or_malformed"
    assert calls == 0


def test_ac4_regression_fail_blocks_pr_reviewer():
    # GIVEN adjudication classified the current head as a blocking regression FAIL
    adjudication = _blocking_adjudication(overall_status="regression_fail")

    decision, calls = _simulate_step4_loop(adjudication_result=adjudication)

    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "adjudication_blocking_true"
    assert calls == 0


def test_ac4_indeterminate_skip_like_result_blocks_pr_reviewer():
    # GIVEN adjudication is indeterminate (analogous to a SKIP that cannot certify PASS)
    adjudication = _blocking_adjudication(overall_status="indeterminate")

    decision, calls = _simulate_step4_loop(adjudication_result=adjudication)

    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "adjudication_blocking_true"
    assert calls == 0


def test_ac4_environment_blocked_fallback_signal_blocks_pr_reviewer():
    # GIVEN adjudication reports an environment-blocked (fallback-adjacent) signal
    adjudication = _blocking_adjudication(overall_status="environment_blocked")

    decision, calls = _simulate_step4_loop(adjudication_result=adjudication)

    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "adjudication_blocking_true"
    assert calls == 0


def test_ac4_nonblocking_flag_with_unresolved_ac_status_still_blocks():
    # GIVEN adjudication.blocking is False but a per_ac entry is not resolved
    # (defensive: a single field flip must not be sufficient to open the gate)
    adjudication = _valid_pass_adjudication()
    adjudication["per_ac"][0]["status"] = "regression_fail"

    decision, calls = _simulate_step4_loop(adjudication_result=adjudication)

    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "adjudication_ac_not_resolved"
    assert calls == 0


# --- AC7: independent VC missing fixture -> pr-reviewer invocation count is 0 ---


def test_ac7_independent_vc_missing_fixture_pr_reviewer_invocation_count_zero():
    # GIVEN the independent Issue VC never produced an adjudication result
    # (e.g. test-runner has not completed yet)
    # WHEN the Step 4 loop iteration runs
    # THEN pr-reviewer invocation count is 0
    _, calls = _simulate_step4_loop(adjudication_result=None)

    assert calls == 0


def test_ac7_test_verdict_comment_alone_does_not_open_the_gate():
    # GIVEN only a TEST_VERDICT comment/artifact exists (legacy diagnostics-only
    # signal) and no VC_ADJUDICATION_RESULT_V1 was bound to the current head
    # WHEN Step 4 evaluates the gate
    # THEN pr-reviewer invocation count stays 0 (Negative Controls: TEST_VERDICT
    # comment alone does not open the Step 4 gate)
    test_verdict_comment_only = {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "result": "PASS",
    }

    decision, calls = _simulate_step4_loop(adjudication_result=test_verdict_comment_only)

    assert decision["invoke_pr_reviewer"] is False
    assert calls == 0
