"""Issue #2467 AC2/AC3: a producer-authorized `runtime_only` skip is only
adjudicated nonblocking PASS when the current-head independent binding
(Issue / PR / current head / reviewed head / diff head / Issue body digest /
source integrity) is fully satisfied AND the current side carries an ACTUAL
executed PASS for that (ac, command_hash) -- status/exit_code/fallback/
human-review/stop-condition execution facts, as
adapt_test_verdict_to_current_vc_result() produces from a real test-runner
TEST_VERDICT_MACHINE/v2 report. A baseline skip declaration alone -- even
when echoed unchanged into the "current" payload -- must never be sufficient
(Issue #2467 Outcome: "baseline skip 単独では nonblocking PASS にならない";
PR #2483 REQUEST_CHANGES P0-1: the baseline skip is delegation AUTHORIZATION
only and must never be re-required on the current side). artifact / receipt
/ TEST_VERDICT are optional diagnostic provenance for runtime_only (unlike
pr_review_only) and their absence is not itself a blocking condition (Issue
#2467 In Scope). Issue/PR binding (PR #2483 REQUEST_CHANGES P1) requires
exact equality against caller-supplied expected_issue_number /
expected_pr_number, not merely positive-integer presence.
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

_spec = importlib.util.spec_from_file_location(
    "adjudicate_vc_result_non_regression_gate_runtime_only", SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


HEAD = "c" * 40
STALE_HEAD = "d" * 40
BODY_SHA = "sha256:" + "f" * 64
ALLOWED_PATH = ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"
ISSUE_NUMBER = 2467
PR_NUMBER = 2469


def _runtime_only_baseline_item(ac: str, *, command_hash: str, exit_code: int | None = None) -> dict[str, Any]:
    """Canonical baseline producer-skip envelope (delegation authorization
    only -- PR #2483 REQUEST_CHANGES P0-1)."""
    return {
        "ac": ac,
        "command_hash": command_hash,
        "raw_command": "echo runtime-only-fixture",
        "runner": "skipped",
        "exit_code": exit_code,
        "classification": "skipped",
        "category": "preflight_scope_runtime_only",
        "decision": "go",
        "scope_class": "runtime_only",
        "verification_owner": "impl-review-loop",
        "deferred_reason": "VC marked runtime_only; verification deferred to post-implementation runtime",
        "runtime_verification_required": True,
        "failure_keys": [],
    }


def _runtime_only_executed_item(
    ac: str,
    *,
    command_hash: str,
    exit_code: int = 0,
    status: str = "pass",
    fallback_detected: bool = False,
    human_review_required: bool = False,
    stop_condition_triggered: bool = False,
) -> dict[str, Any]:
    """Real test-runner-shaped CURRENT execution result for a runtime_only
    (ac, command_hash) -- what adapt_test_verdict_to_current_vc_result()
    produces from a TEST_VERDICT_MACHINE/v2 runtime_ac_results[] entry (PR
    #2483 REQUEST_CHANGES P0-1)."""
    return {
        "ac": ac,
        "command_hash": command_hash,
        "raw_command": "echo runtime-only-fixture",
        "exit_code": exit_code,
        "status": status,
        "fallback_detected": fallback_detected,
        "human_review_required": human_review_required,
        "stop_condition_triggered": stop_condition_triggered,
        "failure_keys": [],
    }


def _regular_item(
    ac: str,
    *,
    command_hash: str,
    classification: str,
    decision: str = "go",
    exit_code: int | None,
) -> dict[str, Any]:
    return {
        "ac": ac,
        "command_hash": command_hash,
        "raw_command": "rg -q fixture tracked.txt",
        "runner": "exec",
        "exit_code": exit_code,
        "classification": classification,
        "category": "regression_gate",
        "decision": decision,
        "scope_class": None,
        "failure_keys": [],
    }


def _contract_snapshot(
    items: list[dict[str, Any]], *, body_sha256: str = BODY_SHA, status: str = "go"
) -> dict[str, Any]:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": status,
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": items}},
    }


def _current_vc_result(
    items: list[dict[str, Any]],
    *,
    issue: Any = ISSUE_NUMBER,
    head_sha: str = HEAD,
    reviewed_head_sha: str = HEAD,
    body_sha256: str = BODY_SHA,
    status: str = "pass",
    errors: list[str] | None = None,
    fallback_detected: bool = False,
    human_review_required: bool = False,
    stop_condition_triggered: bool = False,
    generated_at: str | None = "2026-09-02T00:00:00Z",
) -> dict[str, Any]:
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": issue,
        "generated_at": generated_at,
        "status": status,
        "errors": errors if errors is not None else [],
        "fallback_detected": fallback_detected,
        "human_review_required": human_review_required,
        "stop_condition_triggered": stop_condition_triggered,
        "source": {"body_sha256": body_sha256},
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "results": items,
    }


def _diff_summary(
    *, head_sha: str = HEAD, pr_number: Any = PR_NUMBER, changed_paths: list[str] | None = None
) -> dict[str, Any]:
    return {
        "changed_paths": changed_paths if changed_paths is not None else [ALLOWED_PATH],
        "head_sha": head_sha,
        "pr_number": pr_number,
    }


# --- AC2/AC3: current-head independent binding is required -----------------


def test_non_regression_scope_runtime_only_requires_current_head_binding():
    # GIVEN a producer-authorized runtime_only skip in baseline and a REAL
    # executed PASS for that same (ac, command_hash) in current, but the
    # current head does NOT match the diff head (no fresh current-head
    # independent binding was actually established)
    # WHEN adjudicated
    # THEN the AC is NOT resolved nonblocking PASS -- it fails closed
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "1" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "1" * 64)
    baseline = _contract_snapshot([baseline_item])
    current = _current_vc_result([current_item], head_sha=HEAD, reviewed_head_sha=HEAD)
    drifted_diff_summary = _diff_summary(head_sha=STALE_HEAD)

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=drifted_diff_summary,
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_head_binding_mismatch"]

    # WHEN the current-head independent binding is fully established (diff
    # head matches current/reviewed head, contract snapshot is go, Issue/PR
    # binding present, and the current side is a real executed PASS)
    # THEN the same AC now resolves nonblocking PASS and stays in per_ac
    certified = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=_diff_summary(head_sha=HEAD),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert certified["overall_status"] == "pass"
    assert certified["blocking"] is False
    assert certified["per_ac"][0]["reason_code"] == "runtime_only_current_head_binding_pass"


def test_baseline_skip_echoed_unchanged_as_current_is_not_executed_pass():
    # GIVEN the SAME baseline skip envelope echoed unchanged as "current"
    # (i.e. no real per-command execution happened at all -- the delegated
    # command was never actually run)
    # WHEN adjudicated
    # THEN nonblocking PASS is refused -- the baseline skip is delegation
    # authorization only, never current-head execution evidence (PR #2483
    # REQUEST_CHANGES P0-1; required negative coverage: "baseline skip only
    # (current runtime command 未実行)")
    item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "2" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC1"]


def test_baseline_skip_alone_does_not_certify_nonblocking_pass():
    # GIVEN a real executed PASS on the current side, but the baseline
    # contract snapshot itself is not "go" (no actual current-head
    # certification)
    # WHEN adjudicated
    # THEN nonblocking PASS is refused (baseline skip alone is insufficient)
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "2" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "2" * 64)
    baseline = _contract_snapshot([baseline_item], status="human_judgment")
    current = _current_vc_result([current_item])

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_contract_not_go"]


def test_runtime_only_missing_artifact_or_test_verdict_is_not_blocking():
    # GIVEN a fully-certified runtime_only current-head binding (real
    # executed PASS) with NO test_verdict/artifact/receipt supplied at all
    # WHEN adjudicated
    # THEN the missing artifact/receipt/TEST_VERDICT is not itself a
    # blocking condition (Issue #2467 In Scope: optional diagnostic
    # provenance only)
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "3" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "3" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
        test_verdict=None,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False


def test_runtime_only_missing_issue_number_binding_fails_closed():
    # GIVEN current_vc_result has no issue number bound
    # WHEN adjudicated
    # THEN it fails closed on the Issue binding requirement
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "4" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "4" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item], issue=None),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_issue_number_missing"]


def test_runtime_only_issue_number_mismatch_fails_closed():
    # GIVEN current_vc_result declares a POSITIVE but WRONG issue number
    # (PR #2483 REQUEST_CHANGES P1: exact-value equality, not just presence)
    # WHEN adjudicated
    # THEN it fails closed on the Issue binding equality requirement
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "4" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "4" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item], issue=9999),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_issue_number_mismatch"]


def test_runtime_only_missing_pr_number_binding_fails_closed():
    # GIVEN diff_summary has no PR number bound
    # WHEN adjudicated
    # THEN it fails closed on the PR binding requirement
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "5" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "5" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(pr_number=None),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_pr_number_missing"]


def test_runtime_only_pr_number_mismatch_fails_closed():
    # GIVEN diff_summary declares a POSITIVE but WRONG PR number
    # (PR #2483 REQUEST_CHANGES P1: exact-value equality, not just presence)
    # WHEN adjudicated
    # THEN it fails closed on the PR binding equality requirement
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "5" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "5" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(pr_number=9999),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_pr_number_mismatch"]


def test_runtime_only_stale_contract_body_digest_fails_closed():
    # GIVEN the current_vc_result's source body digest does not match the
    # live contract snapshot digest (stale Issue body binding)
    # WHEN adjudicated
    # THEN it fails closed
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "6" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "6" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item], body_sha256="sha256:" + "1" * 64),
        current_vc_result=_current_vc_result([current_item], body_sha256="sha256:" + "2" * 64),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_source_body_sha256_mismatch"]


def test_runtime_only_fallback_detected_fails_closed():
    # GIVEN the current_vc_result (top-level) reports a fallback was
    # detected, even though the per-command execution facts look fine
    # WHEN adjudicated
    # THEN it fails closed regardless of the per-command execution PASS
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "7" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "7" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item], fallback_detected=True),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_fallback_detected"]


def test_runtime_only_per_command_fallback_detected_fails_closed():
    # GIVEN the per-command execution facts themselves report
    # fallback_detected -- as adapt_test_verdict_to_current_vc_result()
    # would carry through from a real TEST_VERDICT runtime_ac_results[]
    # entry -- even though the top-level payload does not
    # WHEN adjudicated
    # THEN the AC is rejected via the execution-not-pass gate (never
    # silently accepted as an executed PASS)
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "7" * 64)
    current_item = _runtime_only_executed_item(
        "AC1", command_hash="sha256:" + "7" * 64, fallback_detected=True
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC1"]


def test_runtime_only_human_review_required_fails_closed():
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "8" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "8" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item], human_review_required=True),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_human_review_required"]


def test_runtime_only_stop_condition_triggered_fails_closed():
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "9" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "9" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item], stop_condition_triggered=True),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_stop_condition_triggered"]


def test_runtime_only_changed_path_outside_allowed_paths_fails_closed():
    # GIVEN the diff summary changed an out-of-scope path not covered by
    # Allowed Paths
    # WHEN adjudicated
    # THEN source-integrity certification fails closed
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "a" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "a" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(changed_paths=["outside/scope.py"]),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_changed_paths_not_certified"]


def test_runtime_only_current_execution_exit_code_nonzero_fails_closed():
    # GIVEN the current side's per-command exit_code is nonzero (the
    # delegated command was actually run but failed)
    # WHEN adjudicated
    # THEN it is rejected as not-executed-PASS, distinct from a coverage or
    # authorization error (PR #2483 REQUEST_CHANGES required negative
    # coverage: "exit_code != 0")
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "b" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "b" * 64, exit_code=1)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC1"]


def test_runtime_only_current_execution_status_not_pass_fails_closed():
    # GIVEN the current side's per-command status is not "pass" even though
    # exit_code happens to be 0 (e.g. a SKIP recorded with exit_code
    # unset/0)
    # WHEN adjudicated
    # THEN it is rejected as not-executed-PASS (PR #2483 REQUEST_CHANGES
    # required negative coverage: "status != pass")
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "c" * 64)
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "c" * 64, status="skip")

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC1"]


# --- AC5 non-regression companion: coverage completeness --------------------


def test_runtime_only_requires_complete_coverage_rejects_missing_skip():
    # GIVEN baseline has a regular AC plus a runtime_only skip AC, but
    # current only reports the regular AC (skip AC missing entirely)
    # WHEN adjudicated
    # THEN it fails closed via the runtime_only-specific coverage mismatch
    # (distinguished from pr_review_only_coverage_mismatch, Issue #2467 AC5)
    baseline_items = [
        _regular_item(
            "AC1", command_hash="sha256:" + "b" * 64, classification="expected_fail", exit_code=1
        ),
        _runtime_only_baseline_item("AC2", command_hash="sha256:" + "c" * 64),
    ]
    current = _current_vc_result(
        [
            _regular_item(
                "AC1", command_hash="sha256:" + "b" * 64, classification="expected_pass", exit_code=0
            )
        ]
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot(baseline_items),
        current_vc_result=current,
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_coverage_mismatch"]


def test_runtime_only_current_command_hash_mismatch_is_coverage_error():
    # GIVEN baseline declares a canonical runtime_only skip for
    # (AC1, hash_d), but the current payload's matching AC has a DIFFERENT
    # command_hash (the executed command does not correspond to the
    # authorized one)
    # WHEN adjudicated
    # THEN the true authorized (ac, command_hash) key never gets matched, so
    # this is reported via the coverage-mismatch error, not silently
    # accepted as a substitute execution (PR #2483 REQUEST_CHANGES required
    # negative coverage: "command hash 不一致")
    baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "d" * 64)
    mismatched_current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "e" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([mismatched_current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_coverage_mismatch"]


def test_runtime_only_malformed_baseline_authorization_is_rejected():
    # GIVEN the BASELINE envelope itself is malformed (missing the
    # runtime_verification_required=True marker of the canonical producer
    # skip authorization), even though the current side is a real executed
    # PASS
    # WHEN adjudicated
    # THEN it is never recognized as an authorized runtime_only delegation
    # at all (PR #2483 REQUEST_CHANGES required negative coverage:
    # "malformed canonical producer authorization")
    malformed_baseline_item = _runtime_only_baseline_item("AC1", command_hash="sha256:" + "d" * 64)
    malformed_baseline_item["runtime_verification_required"] = False
    current_item = _runtime_only_executed_item("AC1", command_hash="sha256:" + "d" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([malformed_baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]


def test_pr_review_only_coverage_mismatch_is_unaffected_by_runtime_only_addition():
    # Non-regression: a pure pr_review_only coverage-mismatch scenario (no
    # runtime_only involved at all) must keep returning the exact original
    # error string it always has.
    baseline_items = [
        _regular_item(
            "AC1", command_hash="sha256:" + "e" * 64, classification="expected_fail", exit_code=1
        ),
        {
            "ac": "AC2",
            "command_hash": "sha256:" + "f" * 64,
            "raw_command": "rg -q fixture tracked.txt",
            "runner": "skipped",
            "exit_code": None,
            "classification": "skipped",
            "category": "preflight_scope_pr_review_only",
            "decision": "go",
            "scope_class": "pr_review_only",
            "verification_owner": "pr-review-judge",
            "deferred_reason": "VC marked pr_review_only; verification deferred to PR review",
            "runtime_verification_required": False,
            "failure_keys": [],
        },
    ]
    current = _current_vc_result(
        [
            _regular_item(
                "AC1", command_hash="sha256:" + "e" * 64, classification="expected_pass", exit_code=0
            )
        ]
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot(baseline_items),
        current_vc_result=current,
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["errors"] == ["pr_review_only_coverage_mismatch"]
