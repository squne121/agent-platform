"""Issue #2467 AC1: adjudicate_vc_result() must recognize only the exact
canonical `runtime_only` producer-skip envelope
(`runner=skipped`/`classification=skipped`/`category=preflight_scope_runtime_only`/
`decision=go`/`scope_class=runtime_only`/`verification_owner=impl-review-loop`/
non-empty `deferred_reason`/`runtime_verification_required=true`) as a
runtime_only delegation candidate. This module does not parse raw marker
text (`# preflight-scope: runtime_only`) itself -- that remains the
producer's (`baseline_vc_preflight.py`) responsibility; only the structured
envelope fields are consulted here (Issue #2467 In Scope).

PR #2483 REQUEST_CHANGES (P0-1) fix: the canonical envelope is recognized on
the BASELINE side only, as delegation AUTHORIZATION for post-implementation
execution. The CURRENT side must instead carry an ACTUAL executed PASS for
that same (ac, command_hash) -- status == "pass", exit_code == 0, and no
fallback/human-review/stop-condition flags -- exactly what
adapt_test_verdict_to_current_vc_result() produces from a real test-runner
TEST_VERDICT_MACHINE/v2 runtime_ac_results[] entry. Echoing the baseline
skip envelope back unchanged as "current" evidence is never sufficient
(covered by test_adjudicate_vc_result_non_regression_gate_runtime_only.py).
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

_spec = importlib.util.spec_from_file_location("adjudicate_vc_result_runtime_only", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


HEAD = "a" * 40
BODY_SHA = "sha256:" + "e" * 64
ALLOWED_PATH = ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"
ISSUE_NUMBER = 2467
PR_NUMBER = 2469


def _runtime_only_item(
    ac: str,
    *,
    command_hash: str,
    category: str = "preflight_scope_runtime_only",
    classification: str = "skipped",
    decision: str = "go",
    scope_class: str | None = "runtime_only",
    runner: str = "skipped",
    verification_owner: str | None = "impl-review-loop",
    deferred_reason: str | None = "VC marked runtime_only; verification deferred to post-implementation runtime",
    runtime_verification_required: Any = True,
    exit_code: int | None = None,
) -> dict[str, Any]:
    """A canonical baseline producer-skip envelope. Used for the BASELINE
    side only -- this is delegation authorization, not current-head
    execution evidence (PR #2483 REQUEST_CHANGES P0-1)."""
    return {
        "ac": ac,
        "command_hash": command_hash,
        "raw_command": "echo runtime-only-fixture",
        "runner": runner,
        "exit_code": exit_code,
        "classification": classification,
        "category": category,
        "decision": decision,
        "scope_class": scope_class,
        "verification_owner": verification_owner,
        "deferred_reason": deferred_reason,
        "runtime_verification_required": runtime_verification_required,
        "failure_keys": [],
    }


def _runtime_only_executed_pass_item(ac: str, *, command_hash: str) -> dict[str, Any]:
    """A real test-runner-shaped CURRENT execution result for a
    runtime_only (ac, command_hash) -- what
    adapt_test_verdict_to_current_vc_result() produces from a
    TEST_VERDICT_MACHINE/v2 runtime_ac_results[] entry (PR #2483
    REQUEST_CHANGES P0-1)."""
    return {
        "ac": ac,
        "command_hash": command_hash,
        "raw_command": "echo runtime-only-fixture",
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "failure_keys": [],
    }


def _contract_snapshot(items: list[dict[str, Any]], *, body_sha256: str = BODY_SHA) -> dict[str, Any]:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": items}},
    }


def _current_vc_result(
    items: list[dict[str, Any]],
    *,
    issue: int = ISSUE_NUMBER,
    head_sha: str = HEAD,
    reviewed_head_sha: str = HEAD,
    body_sha256: str = BODY_SHA,
    status: str = "pass",
    errors: list[str] | None = None,
    fallback_detected: bool = False,
    human_review_required: bool = False,
    stop_condition_triggered: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": issue,
        "generated_at": "2026-09-02T00:00:00Z",
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
    *, head_sha: str = HEAD, pr_number: int = PR_NUMBER, changed_paths: list[str] | None = None
) -> dict[str, Any]:
    return {
        "changed_paths": changed_paths if changed_paths is not None else [ALLOWED_PATH],
        "head_sha": head_sha,
        "pr_number": pr_number,
    }


# --- AC1: exact canonical envelope recognized ------------------------------


def test_runtime_only_producer_envelope_accepted():
    # GIVEN a baseline snapshot declaring the exact canonical runtime_only
    # producer-skip envelope (delegation authorization) AND a current-head
    # result that is a REAL executed PASS for that same (ac, command_hash)
    # (as adapt_test_verdict_to_current_vc_result() would produce from a
    # real test-runner report), bound to a fresh, certified current head
    # WHEN adjudicate_vc_result() runs
    # THEN the AC is adjudicated nonblocking PASS, appears in per_ac (not
    # excluded), via the runtime_only current-head binding reason code
    baseline_item = _runtime_only_item("AC1", command_hash="sha256:" + "1" * 64)
    current_item = _runtime_only_executed_pass_item("AC1", command_hash="sha256:" + "1" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert len(result["per_ac"]) == 1
    assert result["per_ac"][0]["ac"] == "AC1"
    assert result["per_ac"][0]["reason_code"] == "runtime_only_current_head_binding_pass"


def test_runtime_only_echoed_baseline_skip_as_current_is_not_executed_pass():
    # GIVEN a baseline canonical skip AND the SAME skip envelope echoed back
    # unchanged as "current" (no real execution happened)
    # WHEN adjudicated
    # THEN this is rejected -- the baseline skip is authorization only, not
    # current-head execution evidence (PR #2483 REQUEST_CHANGES P0-1)
    item = _runtime_only_item("AC1", command_hash="sha256:" + "9" * 64)

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


def test_runtime_only_wrong_verification_owner_is_not_recognized():
    # GIVEN a skip item claiming scope_class=runtime_only but with the
    # pr_review_only owner (malformed cross-scope envelope)
    # WHEN adjudicated
    # THEN it is NOT recognized as a runtime_only producer skip and fails
    # closed via the generic unsupported-classification guard
    item = _runtime_only_item(
        "AC1", command_hash="sha256:" + "2" * 64, verification_owner="pr-review-judge"
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]


def test_runtime_only_missing_deferred_reason_is_not_recognized():
    # GIVEN a skip item missing the (non-empty) deferred_reason field
    # WHEN adjudicated
    # THEN it is not recognized as a runtime_only producer skip
    item = _runtime_only_item("AC1", command_hash="sha256:" + "3" * 64, deferred_reason=None)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]


def test_runtime_only_runtime_verification_required_false_is_not_recognized():
    # GIVEN a skip item with runtime_verification_required=False (this is
    # the pr_review_only polarity, not runtime_only's)
    # WHEN adjudicated
    # THEN it is not recognized as a runtime_only producer skip
    item = _runtime_only_item(
        "AC1", command_hash="sha256:" + "4" * 64, runtime_verification_required=False
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]


def test_runtime_only_wrong_category_is_not_recognized():
    # GIVEN a skip item with a mismatched category (not
    # preflight_scope_runtime_only)
    # WHEN adjudicated
    # THEN it is not recognized as a runtime_only producer skip
    item = _runtime_only_item("AC1", command_hash="sha256:" + "5" * 64, category="unknown")

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]


def test_runtime_only_does_not_extend_pr_review_only_authorization():
    # GIVEN a skip item declaring pr_review_only's owner/category alongside
    # scope_class=runtime_only (an attempt to cross-authorize)
    # WHEN adjudicated
    # THEN neither recognizer accepts it -- runtime_only does not extend the
    # existing pr_review_only authorized scope (Issue #2467 Out of Scope)
    item = _runtime_only_item(
        "AC1",
        command_hash="sha256:" + "6" * 64,
        category="preflight_scope_pr_review_only",
        verification_owner="pr-review-judge",
        runtime_verification_required=False,
    )
    # scope_class is still "runtime_only" -- pr_review_only recognizer checks
    # scope_class == "pr_review_only", so this must be rejected by both.
    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]
