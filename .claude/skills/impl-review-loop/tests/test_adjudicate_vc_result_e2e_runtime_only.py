"""Issue #2467 AC7: an e2e fixture shaped like #2432 (10 Acceptance
Criteria: 9 regression-gate VCs + 1 runtime_only VC) proves that the
CANONICAL production lane -- REAL `baseline_vc_preflight.py` baseline skip
(delegation authorization) -> test-runner-shaped `TEST_VERDICT_MACHINE/v2`
report (real per-command execution facts) ->
`adapt_test_verdict_to_current_vc_result()` -> `adjudicate_vc_result()` ->
`evaluate_step4_vc_gate()` -- opens the Step 4 gate only when the
current-head independent binding AND the runtime_only command's actual
executed PASS are genuinely established, and that a baseline-only run, an
un-executed skip, a failed/incomplete execution, a wrong command hash, a
wrong Issue/PR number, a stale head, a wrong body digest, and a malformed
baseline authorization all stay blocked.

PR #2483 REQUEST_CHANGES (P0-1/P0-2) fix: the previous version of this file
fed the producer's own `--evidence-mode current-head` (baseline-shaped)
output directly into adjudicate_vc_result() as "current" evidence and
asserted that the runtime_only AC is EXCLUDED from per_ac -- exactly the
inverted/hollow-pass shape the review flagged. This version instead builds
the "current" side the way Step 2 actually does in production: a real
test-runner TEST_VERDICT_MACHINE/v2 report converted through the canonical
adapter, and asserts the runtime_only AC stays INSIDE per_ac in the live
Issue's literal declaration order.

The baseline snapshot is still generated from the REAL
`baseline_vc_preflight.py` producer over a real git repository -- only the
"current" side construction changed (Out of Scope: `baseline_vc_preflight.py`
itself is not modified by Issue #2467).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
ADJUDICATE_SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "adjudicate_vc_result.py"
)
PRODUCER_SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "issue-contract-review"
    / "scripts"
    / "baseline_vc_preflight.py"
)

_spec = importlib.util.spec_from_file_location(
    "adjudicate_vc_result_e2e_runtime_only", ADJUDICATE_SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


# 9 regression-gate ACs (AC1-AC9) + 1 runtime_only AC (AC10) -- shaped like
# #2432's ~10-AC contract (Issue #2467 AC7).
_WORDS = [f"needle{i}" for i in range(1, 10)]

ISSUE_NUMBER = 2467
PR_NUMBER = 2469


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)


def _commit_all(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def _run_producer(
    *,
    repo: Path,
    body_file: Path,
    evidence_mode: str | None = None,
    reviewed_head_sha: str | None = None,
) -> dict:
    argv = [
        sys.executable,
        str(PRODUCER_SCRIPT_PATH),
        "--body-file",
        str(body_file),
        "--cwd",
        str(repo),
        "--format",
        "json",
        "--issue",
        str(ISSUE_NUMBER),
        "--repo",
        "squne121/agent-platform",
    ]
    if evidence_mode is not None:
        argv += ["--evidence-mode", evidence_mode]
    if reviewed_head_sha is not None:
        argv += ["--reviewed-head-sha", reviewed_head_sha]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert completed.stdout, f"producer emitted no stdout: {completed.stderr}"
    return json.loads(completed.stdout)


def _git_diff_changed_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only", base_sha, head_sha],
        text=True,
    )
    return [line for line in output.splitlines() if line.strip()]


def _body_text() -> str:
    lines = ["## Allowed Paths", "- tracked.txt", "", "## Verification Commands", "", "```bash"]
    for idx, word in enumerate(_WORDS, start=1):
        lines.append(f"# AC{idx}")
        lines.append(f"$ rg -q {word} tracked.txt")
        lines.append("")
    lines.append("# AC10")
    lines.append("# preflight-scope: runtime_only")
    lines.append("$ echo runtime-check")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _make_fixture_repo(tmp_path: Path) -> tuple[Path, Path, str, str, dict]:
    """Build the shared baseline -> implementation commit pair and return
    (repo, body_file, base_sha, head_sha, baseline_result). The REAL baseline
    producer snapshot is captured while the working tree is still AT the
    baseline commit (before the implementation commit is created), matching
    the existing E2E fixtures in test_adjudicate_vc_result_non_regression_gate.py."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    body_file = tmp_path / "issue-2467-body.md"
    body_file.write_text(_body_text(), encoding="utf-8")

    (repo / "tracked.txt").write_text("placeholder\n", encoding="utf-8")
    base_sha = _commit_all(repo, "baseline")

    baseline_result = _run_producer(repo=repo, body_file=body_file)
    for entry in baseline_result["results"][:9]:
        assert entry["classification"] == "expected_fail"
    assert baseline_result["results"][9]["scope_class"] == "runtime_only"
    assert baseline_result["results"][9]["runtime_verification_required"] is True

    (repo / "tracked.txt").write_text(" ".join(_WORDS) + "\n", encoding="utf-8")
    head_sha = _commit_all(repo, "implement AC1-AC10")
    return repo, body_file, base_sha, head_sha, baseline_result


def _contract_snapshot(baseline_result: dict) -> dict:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": baseline_result["results"]}},
    }


# --- test-runner-shaped TEST_VERDICT_MACHINE/v2 construction ---------------
#
# Issue #2467 P0-1 review fix: the "current" evidence for a real production
# run is a TEST_VERDICT_MACHINE/v2 report from test-runner, NOT another
# baseline_vc_preflight.py producer-skip run. This builds that report
# in-test (test-runner itself is out of scope for #2467; only the
# adjudicator/adapter contract is) and converts it through the canonical
# adapter, exactly as Step 2 does in production
# (step-2-verification.md / adjudicate_vc_result.py `adapt` subcommand).


def _executed_ac_result(
    ac: str,
    command_hash: str,
    *,
    command: str = "echo x",
    exit_code: int = 0,
    status: str = "pass",
    fallback_detected: bool = False,
    human_review_required: bool = False,
    stop_condition_triggered: bool = False,
) -> dict[str, Any]:
    return {
        "ac": ac,
        "command": command,
        "command_hash": command_hash,
        "exit_code": exit_code,
        "status": status,
        "fallback_detected": fallback_detected,
        "artifact_present": "not_required",
        "human_review_required": human_review_required,
        "stop_condition_triggered": stop_condition_triggered,
        "notes": "",
    }


def _test_verdict_payload(
    *,
    runtime_ac_results: list[dict[str, Any]],
    head_sha: str,
    reviewed_head_sha: str,
    diff_head_sha: str,
    contract_body_sha256: str,
    issue_number: int = ISSUE_NUMBER,
    pr_number: int = PR_NUMBER,
    generated_at: str = "2026-09-02T00:00:00Z",
) -> dict[str, Any]:
    return {
        "schema": mod.TEST_VERDICT_SCHEMA,
        "producer_kind": "test-runner",
        "repository": "squne121/agent-platform",
        "issue_number": issue_number,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "diff_head_sha": diff_head_sha,
        "contract_body_sha256": contract_body_sha256,
        "generated_at": generated_at,
        "result": "PASS",
        "runtime_ac_results": runtime_ac_results,
    }


def _current_vc_result_from_test_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    converted, errors = mod.adapt_test_verdict_to_current_vc_result(payload)
    assert errors == [], errors
    assert converted is not None
    return converted


def _all_ac_results(baseline_result: dict) -> list[dict[str, Any]]:
    """Build a 10-entry TEST_VERDICT.runtime_ac_results[] shaped as a real
    test-runner would report: AC1-AC9 (ordinary regression-gate commands)
    plus AC10 (the runtime_only command test-runner actually executes post-
    implementation, per Issue #2467 Outcome), all using the SAME
    command_hash the live Issue's Verification Commands declare (obtained
    from the real baseline producer's output, not re-derived)."""
    return [
        _executed_ac_result(entry["ac"], entry["command_hash"], command=entry["raw_command"])
        for entry in baseline_result["results"]
    ]


def _expected_command_hashes_from_baseline(baseline_result: dict) -> list[str]:
    """Independently derive the expected, ordered command-hash binding
    tuple from the live Issue's declared Verification Commands (via the
    REAL baseline producer output) -- NOT from adjudicate_vc_result()'s own
    per_ac output, to avoid the circular binding test the review flagged."""
    return [entry["command_hash"] for entry in baseline_result["results"]]


# --- AC7 positive path -------------------------------------------------


def test_e2e_2432_shaped_fixture_runtime_only_ac_opens_step4_gate_only_with_current_head_binding(
    tmp_path: Path,
) -> None:
    """GIVEN a real 10-AC (#2432-shaped) fixture where 9 ACs are ordinary
    regression-gate VCs and 1 AC (AC10) carries the canonical runtime_only
    producer-skip envelope in the BASELINE (delegation authorization only)
    WHEN a real test-runner-shaped TEST_VERDICT_MACHINE/v2 report -- with an
    ACTUAL executed PASS for every AC including AC10 -- is adapted and fed
    into adjudicate_vc_result() together with the real diff summary and
    Issue body digest
    THEN the whole adjudication is nonblocking PASS, ALL 10 ACs (including
    AC10) appear in per_ac in the live Issue's literal declaration order,
    AC10 resolves via runtime_only_current_head_binding_pass, and
    evaluate_step4_vc_gate() opens (invoke_pr_reviewer is True) against
    expected command hashes derived independently from the live Issue (not
    from the adjudication output itself)."""
    repo, body_file, base_sha, head_sha, baseline_result = _make_fixture_repo(tmp_path)

    test_verdict = _test_verdict_payload(
        runtime_ac_results=_all_ac_results(baseline_result),
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current_vc_result = _current_vc_result_from_test_verdict(test_verdict)
    assert current_vc_result["status"] == "pass"
    assert current_vc_result["issue"] == ISSUE_NUMBER
    assert current_vc_result["pr_number"] == PR_NUMBER

    changed_paths = _git_diff_changed_paths(repo, base_sha, head_sha)
    assert changed_paths == ["tracked.txt"]
    diff_summary = {
        "changed_paths": changed_paths,
        "head_sha": head_sha,
        "pr_number": PR_NUMBER,
    }

    contract_snapshot = _contract_snapshot(baseline_result)

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    # Issue #2467 P0-2: the runtime_only AC (AC10) stays INSIDE per_ac,
    # alongside the 9 executed regression-gate ACs, in the live Issue's
    # literal declaration order -- never dropped, never re-sorted.
    assert len(result["per_ac"]) == 10
    assert [entry["ac"] for entry in result["per_ac"]] == [f"AC{i}" for i in range(1, 11)]
    for entry in result["per_ac"][:9]:
        assert entry["reason_code"] == "expected_fail_resolved_on_current_head"
    assert result["per_ac"][9]["reason_code"] == "runtime_only_current_head_binding_pass"

    expected_command_hashes = _expected_command_hashes_from_baseline(baseline_result)
    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=contract_snapshot["body_sha256"],
        expected_command_hashes=expected_command_hashes,
    )
    assert gate["invoke_pr_reviewer"] is True
    assert gate["reason_code"] is None

    # Isolate AC10 alone (the runtime_only-only shape) to exercise the
    # dedicated runtime_only_current_head_binding_pass synthesis path in
    # isolation and confirm it independently opens the Step 4 gate for its
    # own binding tuple.
    runtime_only_baseline_item = baseline_result["results"][9]
    runtime_only_only_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": [runtime_only_baseline_item]}},
    }
    isolated_test_verdict = _test_verdict_payload(
        runtime_ac_results=[test_verdict["runtime_ac_results"][9]],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    isolated_current = _current_vc_result_from_test_verdict(isolated_test_verdict)

    isolated_result = mod.adjudicate_vc_result(
        contract_snapshot=runtime_only_only_snapshot,
        current_vc_result=isolated_current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert isolated_result["overall_status"] == "pass"
    assert isolated_result["blocking"] is False
    assert len(isolated_result["per_ac"]) == 1
    assert isolated_result["per_ac"][0]["ac"] == "AC10"
    assert isolated_result["per_ac"][0]["reason_code"] == "runtime_only_current_head_binding_pass"

    isolated_gate = mod.evaluate_step4_vc_gate(
        isolated_result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=runtime_only_only_snapshot["body_sha256"],
        expected_command_hashes=[runtime_only_baseline_item["command_hash"]],
    )
    assert isolated_gate["invoke_pr_reviewer"] is True
    assert isolated_gate["reason_code"] is None


# --- AC7 negative path 1: baseline-only never opens the gate ------------


def test_e2e_baseline_only_run_never_opens_step4_gate(tmp_path: Path) -> None:
    """GIVEN the SAME real fixture, but "current" is actually the producer's
    default BASELINE-mode run (not a certified current-head run, and not a
    real test-runner execution)
    WHEN fed into adjudicate_vc_result()
    THEN the adjudication is never nonblocking PASS and the Step 4 gate
    never opens (a baseline skip declaration alone is insufficient)."""
    repo, body_file, _base_sha, _head_sha, baseline_result = _make_fixture_repo(tmp_path)

    contract_snapshot = _contract_snapshot(baseline_result)

    # Re-run the producer in baseline (non current-head) mode over the exact
    # same (already-implemented) tree -- this is the actual current_vc_result
    # payload the SubAgent would see if it fabricated a "current" run without
    # a real current-head binding: its own top-level status is not "pass".
    baseline_only_current = _run_producer(repo=repo, body_file=body_file)
    assert baseline_only_current["status"] != "pass"

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=baseline_only_current,
        diff_summary={"changed_paths": ["tracked.txt"], "head_sha": None, "pr_number": PR_NUMBER},
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True

    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha="does-not-matter",
        expected_contract_body_sha256=contract_snapshot["body_sha256"],
        expected_command_hashes=[],
    )
    assert gate["invoke_pr_reviewer"] is False


def test_e2e_runtime_only_skip_only_echoed_as_current_is_not_executed_pass(tmp_path: Path) -> None:
    """GIVEN the isolated AC10-only fixture, but the "current" side is the
    SAME baseline canonical skip envelope echoed back unchanged (the
    delegated command was never actually run by test-runner)
    WHEN fed into adjudicate_vc_result()
    THEN the AC is rejected via the execution-not-pass gate and the Step 4
    gate never opens (PR #2483 REQUEST_CHANGES required negative coverage:
    "baseline skip only, current runtime command 未実行")."""
    _repo, _body_file, _base_sha, head_sha, baseline_result = _make_fixture_repo(tmp_path)

    runtime_only_baseline_item = baseline_result["results"][9]
    runtime_only_only_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": [runtime_only_baseline_item]}},
    }
    echoed_current = {
        "schema": "baseline_vc_preflight/v1",
        "issue": ISSUE_NUMBER,
        "generated_at": "2026-09-02T00:00:00Z",
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": baseline_result["source"]["body_sha256"]},
        "head_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "results": [runtime_only_baseline_item],
    }
    diff_summary = {"changed_paths": ["tracked.txt"], "head_sha": head_sha, "pr_number": PR_NUMBER}

    result = mod.adjudicate_vc_result(
        contract_snapshot=runtime_only_only_snapshot,
        current_vc_result=echoed_current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC10"]

    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=runtime_only_only_snapshot["body_sha256"],
        expected_command_hashes=[],
    )
    assert gate["invoke_pr_reviewer"] is False


# --- AC7 negative path 2: isolated AC10, executed but non-PASS ----------


def _isolated_fixture(tmp_path: Path):
    repo, body_file, base_sha, head_sha, baseline_result = _make_fixture_repo(tmp_path)
    runtime_only_baseline_item = baseline_result["results"][9]
    runtime_only_only_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": [runtime_only_baseline_item]}},
    }
    diff_summary = {"changed_paths": ["tracked.txt"], "head_sha": head_sha, "pr_number": PR_NUMBER}
    return runtime_only_baseline_item, runtime_only_only_snapshot, head_sha, diff_summary, baseline_result


def test_e2e_runtime_only_current_execution_exit_code_nonzero_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: exit_code != 0."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[
            _executed_ac_result("AC10", runtime_only_baseline_item["command_hash"], exit_code=1)
        ],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC10"]

    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=snapshot["body_sha256"],
        expected_command_hashes=[],
    )
    assert gate["invoke_pr_reviewer"] is False


def test_e2e_runtime_only_current_status_not_pass_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: status != pass."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[
            _executed_ac_result("AC10", runtime_only_baseline_item["command_hash"], status="fail")
        ],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert gate_never_opens(result, head_sha, snapshot)


def gate_never_opens(result: dict, head_sha: str, snapshot: dict) -> bool:
    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=snapshot["body_sha256"],
        expected_command_hashes=[],
    )
    return gate["invoke_pr_reviewer"] is False


def test_e2e_runtime_only_command_hash_mismatch_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: command hash 不一致."""
    _runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    wrong_hash = "sha256:" + "9" * 64
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[_executed_ac_result("AC10", wrong_hash)],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_coverage_mismatch"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_runtime_only_issue_number_mismatch_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: Issue number 不一致."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[_executed_ac_result("AC10", runtime_only_baseline_item["command_hash"])],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
        issue_number=999999,
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_issue_number_mismatch"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_runtime_only_pr_number_mismatch_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: PR number 不一致."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[_executed_ac_result("AC10", runtime_only_baseline_item["command_hash"])],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)
    wrong_pr_diff_summary = {**diff_summary, "pr_number": 999999}

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=wrong_pr_diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_pr_number_mismatch"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_runtime_only_stale_head_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: stale/wrong head."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[_executed_ac_result("AC10", runtime_only_baseline_item["command_hash"])],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)
    stale_diff_summary = {**diff_summary, "head_sha": "f" * 40}

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=stale_diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_head_binding_mismatch"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_runtime_only_wrong_body_digest_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: wrong body digest."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[_executed_ac_result("AC10", runtime_only_baseline_item["command_hash"])],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256="sha256:" + "0" * 64,
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_source_body_sha256_mismatch"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_runtime_only_fallback_detected_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: fallback_detected."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[
            _executed_ac_result(
                "AC10", runtime_only_baseline_item["command_hash"], fallback_detected=True
            )
        ],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC10"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_runtime_only_human_review_required_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: human_review_required."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[
            _executed_ac_result(
                "AC10", runtime_only_baseline_item["command_hash"], human_review_required=True
            )
        ],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC10"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_runtime_only_stop_condition_triggered_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: stop_condition_triggered."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[
            _executed_ac_result(
                "AC10", runtime_only_baseline_item["command_hash"], stop_condition_triggered=True
            )
        ],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_execution_not_pass:AC10"]
    assert gate_never_opens(result, head_sha, snapshot)


def test_e2e_malformed_baseline_authorization_never_opens_gate(tmp_path: Path) -> None:
    """Required negative coverage: malformed canonical producer
    authorization (the baseline envelope itself is invalid)."""
    runtime_only_baseline_item, snapshot, head_sha, diff_summary, baseline_result = _isolated_fixture(
        tmp_path
    )
    tampered_snapshot = json.loads(json.dumps(snapshot))
    tampered_snapshot["checks"]["vc_preflight"]["classifications"][0]["verification_owner"] = (
        "attacker-controlled"
    )
    test_verdict = _test_verdict_payload(
        runtime_ac_results=[_executed_ac_result("AC10", runtime_only_baseline_item["command_hash"])],
        head_sha=head_sha,
        reviewed_head_sha=head_sha,
        diff_head_sha=head_sha,
        contract_body_sha256=baseline_result["source"]["body_sha256"],
    )
    current = _current_vc_result_from_test_verdict(test_verdict)

    result = mod.adjudicate_vc_result(
        contract_snapshot=tampered_snapshot,
        current_vc_result=current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC10"]
    assert gate_never_opens(result, head_sha, tampered_snapshot)
