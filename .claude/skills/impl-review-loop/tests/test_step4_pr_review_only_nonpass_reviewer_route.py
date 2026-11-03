"""Issue #2916: an EXECUTED non-pass pr_review_only item reaches the reviewer without becoming PASS.

The production entrypoints are driven through subprocesses only: the real
``adjudicate_vc_result.py step4-adjudicate`` CLI and, in a SEPARATE process, the real
``step5-terminal-gate`` CLI. The baseline authority comes from the real
``baseline_vc_preflight.py`` producer and the real ``extract-vc-metadata`` subcommand.
Nothing in the adjudicate / persist / gate chain is mocked, no CI identifier is filled
into a fixture, and no skip / xfail marker is used. Every assertion is on
``reason_code`` / ``invoke_pr_reviewer`` / ``route`` (and the persisted facts), never on a
return code alone.

Four states stay separate:

1. the executed non-pass FACT (recorded verbatim, never rewritten to PASS),
2. reviewer DISPATCH permission (``step4-adjudicate`` exit 0 -- not AC achievement),
3. the reviewer's semantic verdict, and
4. terminal approval (only ``step5-terminal-gate`` exit 0).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
SCRIPT_PATH = SKILL_DIR / "scripts" / "adjudicate_vc_result.py"
PRODUCER_PATH = ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"

# Unique module name: a bare ``import adjudicate_vc_result`` can collide with a same-named
# file in a shared pytest session (sys.modules cache).
_spec = importlib.util.spec_from_file_location("adjudicate_vc_result_step4_nonpass_reviewer_route", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]

ISSUE_NUMBER = 2916
PR_NUMBER = 2990
GENERATED_AT = "2026-10-07T12:00:00Z"
CHANGED_PATHS = ["implemented.txt"]
ALLOWED_PATHS = ["tracked.txt", "implemented.txt", "implemented2.txt"]
DELEGATED = "pr_review_only_nonpass_delegated_to_reviewer"
FLAG = "--delegate-pr-review-only-nonpass"

TRUST_MARKER_KEYS = (
    "workflow_run_id",
    "workflow_run_attempt",
    "check_run_id",
    "artifact",
    "artifact_payload",
    "artifact_payload_sha256",
    "producer_receipt",
    "receipt_sha256",
)

BODY_SINGLE = """## Allowed Paths
- tracked.txt
- implemented.txt

## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt
```
"""

BODY_MIXED = """## Allowed Paths
- tracked.txt
- implemented.txt

## Verification Commands

```bash
# AC1
$ test -f implemented.txt

# AC2
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt
```
"""

BODY_RUNTIME_MIXED = """## Allowed Paths
- tracked.txt
- implemented.txt

## Verification Commands

```bash
# AC1
# preflight-scope: runtime_only
$ rg -q fixture tracked.txt --max-count 1

# AC2
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt
```
"""

# Executed non-pass facts a reviewer may judge (row overrides of a passing row).
DELEGABLE_ROWS: dict[str, dict[str, Any]] = {
    "fail_exit_1": {"exit_code": 1, "status": "fail"},
    "fail_exit_5": {"exit_code": 5, "status": "fail"},
    "skip_exit_77": {"exit_code": 77, "status": "skip"},
    # test-runner classifies a fallback success as FAIL (exit 0 + status fail), never PASS.
    "fail_exit_0_with_fallback": {"exit_code": 0, "status": "fail", "fallback_detected": True},
    "fail_with_fallback": {"exit_code": 1, "status": "fail", "fallback_detected": True},
}


# --- real producer / real metadata extractor ---------------------------------


@dataclass
class Scenario:
    head: str
    body_sha256: str
    producer: dict[str, Any]
    snapshot: dict[str, Any]
    commands: list[dict[str, Any]]
    hashes: list[str]
    kinds: list[str]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _build_scenario(tmp_path_factory: pytest.TempPathFactory, name: str, body: str) -> Scenario:
    base = tmp_path_factory.mktemp(f"scenario_{name}")
    repo = base / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    head = _git(repo, "rev-parse", "HEAD")
    body_file = base / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(PRODUCER_PATH),
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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout, f"producer emitted no stdout: {completed.stderr}"
    producer = json.loads(completed.stdout)
    assert producer["status"] == "pass", producer.get("errors")

    metadata_run = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "extract-vc-metadata", "--body-file", str(body_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    metadata = json.loads(metadata_run.stdout)
    assert metadata["status"] == "ok", metadata
    commands = metadata["commands"]
    assert [(row["ac"], row["command_hash"]) for row in producer["results"]] == [
        (row["ac"], row["command_hash"]) for row in commands
    ]
    kinds = [
        row["scope_class"] if row["scope_class"] in {"pr_review_only", "runtime_only"} else "ordinary"
        for row in producer["results"]
    ]
    snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": producer["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": producer["results"]}},
    }
    return Scenario(
        head=head,
        body_sha256=producer["source"]["body_sha256"],
        producer=producer,
        snapshot=snapshot,
        commands=commands,
        hashes=metadata["command_hashes"],
        kinds=kinds,
    )


@pytest.fixture(scope="module")
def runtime_mixed(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "runtime_mixed", BODY_RUNTIME_MIXED)
    assert scenario.kinds == ["runtime_only", "pr_review_only"]
    return scenario


@pytest.fixture(scope="module")
def single(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "single", BODY_SINGLE)
    assert scenario.kinds == ["pr_review_only"]
    return scenario


@pytest.fixture(scope="module")
def mixed(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "mixed", BODY_MIXED)
    assert scenario.kinds == ["ordinary", "pr_review_only"]
    return scenario


# --- independent test-runner report builders (no trust marker) -----------------


def _row(command: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ac": command["ac"],
        "command": command["raw_command"],
        "command_hash": command["command_hash"],
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "artifact_present": "not_required",
        "human_review_required": False,
        "stop_condition_triggered": False,
        "notes": "",
    }
    row.update(overrides)
    return row


def _report(
    scenario: Scenario,
    *,
    row_overrides: dict[int, dict[str, Any]] | None = None,
    result: str = "FAIL",
    head: str | None = None,
    body_sha256: str | None = None,
    issue_number: Any = ISSUE_NUMBER,
    pr_number: Any = PR_NUMBER,
    **top_level: Any,
) -> dict[str, Any]:
    report_head = head or scenario.head
    report: dict[str, Any] = {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "issue_number": issue_number,
        "pr_number": pr_number,
        "head_sha": report_head,
        "reviewed_head_sha": report_head,
        "diff_head_sha": report_head,
        "contract_body_sha256": body_sha256 if body_sha256 is not None else scenario.body_sha256,
        "generated_at": GENERATED_AT,
        "result": result,
        "runtime_ac_results": [
            _row(command, **((row_overrides or {}).get(index, {}))) for index, command in enumerate(scenario.commands)
        ],
    }
    report.update(top_level)
    return report


def _pr_review_only_index(scenario: Scenario) -> int:
    return scenario.kinds.index("pr_review_only")


def _diff_summary(scenario: Scenario, **overrides: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "head_sha": scenario.head,
        "pr_number": PR_NUMBER,
        "changed_paths": list(CHANGED_PATHS),
    }
    summary.update(overrides)
    return summary


def _reviewer_verdict(head: str, verdict: str = "APPROVE", blockers: list[str] | None = None) -> dict[str, Any]:
    return {"verdict": verdict, "reviewed_head_sha": head, "blockers": blockers or [], "warnings": []}


class Workspace:
    """Caller-side files a root would pass to the real CLIs (no mocks)."""

    def __init__(self, tmp_path: Path, scenario: Scenario) -> None:
        self.dir = tmp_path
        self.dir.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.loop_state = self.dir / "loop_state.json"

    def write(self, name: str, value: Any) -> str:
        path = self.dir / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return str(path)

    def state(self) -> dict[str, Any]:
        return json.loads(self.loop_state.read_text(encoding="utf-8"))

    def run(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *argv], capture_output=True, text=True, check=False
        )
        assert completed.stdout.strip(), f"no stdout: rc={completed.returncode} stderr={completed.stderr}"
        return completed.returncode, json.loads(completed.stdout.strip().splitlines()[-1])

    def binding_args(self, *, head: str | None = None, body: str | None = None, tag: str = "b") -> list[str]:
        return [
            "--loop-state-file",
            str(self.loop_state),
            "--expected-head-sha",
            head or self.scenario.head,
            "--expected-contract-body-sha256",
            body if body is not None else self.scenario.body_sha256,
            "--expected-command-hashes-file",
            self.write(f"hashes_{tag}.json", self.scenario.hashes),
        ]

    def adjudicate(
        self,
        report: Any,
        *,
        delegate: bool = True,
        diff_summary: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        allowed: list[str] | None = None,
        expected_issue: int | None = ISSUE_NUMBER,
        expected_pr: int | None = PR_NUMBER,
        extra_args: tuple[str, ...] = (),
        tag: str = "adj",
    ) -> tuple[int, dict[str, Any]]:
        """Run `step4-adjudicate` (the only reviewer-dispatch entrance)."""
        scenario = self.scenario
        argv = [
            "step4-adjudicate",
            *self.binding_args(tag=tag),
            "--test-verdict-file",
            self.write(f"report_{tag}.json", report),
            "--contract-snapshot-file",
            self.write(f"snapshot_{tag}.json", snapshot if snapshot is not None else scenario.snapshot),
            "--diff-summary-file",
            self.write(f"diff_{tag}.json", diff_summary if diff_summary is not None else _diff_summary(scenario)),
            "--allowed-paths-file",
            self.write(f"allowed_{tag}.json", allowed if allowed is not None else ALLOWED_PATHS),
            *extra_args,
        ]
        if delegate:
            argv.append(FLAG)
        if expected_issue is not None:
            argv += ["--expected-issue-number", str(expected_issue)]
        if expected_pr is not None:
            argv += ["--expected-pr-number", str(expected_pr)]
        return self.run(argv)

    def terminal_gate(
        self,
        *,
        dispatch_seq: int,
        verdict: Any = None,
        verdict_path: str | None = None,
        head: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        head = head or self.scenario.head
        argv = [
            "step5-terminal-gate",
            *self.binding_args(head=head, tag="term"),
            "--reviewer-verdict-file",
            verdict_path
            if verdict_path is not None
            else self.write("reviewer_verdict.json", verdict if verdict is not None else _reviewer_verdict(head)),
            "--live-mergeability-file",
            self.write(
                "live_mergeability.json",
                {"head_sha": head, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
            ),
            "--dispatch-seq",
            str(dispatch_seq),
        ]
        return self.run(argv)

    def step4_gate(self) -> tuple[int, dict[str, Any]]:
        return self.run(["step4-gate", *self.binding_args(tag="gate")])

    def binding_key(self) -> str:
        return mod.step4_binding_key(
            head_sha=self.scenario.head,
            contract_body_sha256=self.scenario.body_sha256,
            command_hashes=self.scenario.hashes,
        )

    def stored(self) -> dict[str, Any]:
        return self.state()["vc_adjudication"][self.binding_key()]


def _assert_no_dispatch(ws: Workspace, rc: int, payload: dict[str, Any], errors: list[str]) -> None:
    """A fail-closed Step 4 outcome: no reviewer dispatch, no persisted adjudication."""
    assert payload["invoke_pr_reviewer"] is False, payload
    assert payload["seq"] is None, payload
    assert payload["reason_code"] == "adjudication_missing_or_malformed", payload
    assert payload["adjudication"]["errors"] == errors, payload
    assert payload["adjudication"]["blocking"] is True, payload
    assert "pr_review_only_nonpass_delegated" not in payload["adjudication"]
    assert rc == 1, payload
    state = ws.state()
    assert "dispatch" not in state
    assert state.get("vc_adjudication", {}) == {}


def _dispatched_with_failure(tmp_path: Path, scenario: Scenario, override: dict[str, Any]) -> Workspace:
    """Workspace where the failing pr_review_only item was delegated and dispatch seq=1 recorded."""
    ws = Workspace(tmp_path, scenario)
    index = _pr_review_only_index(scenario)
    rc, payload = ws.adjudicate(_report(scenario, row_overrides={index: override}))
    assert rc == 0 and payload["invoke_pr_reviewer"] is True and payload["seq"] == 1, payload
    return ws


def _decoded(entry: dict[str, Any]) -> dict[str, Any]:
    facts = {}
    for row in entry["failure_keys"]:
        assert row["kind"] == "pr_review_only_current_execution_fact"
        name, _, raw = row["key"].partition("=")
        facts[name] = json.loads(raw)
    return facts


# --- AC1: facts are persisted lossless and never become PASS -------------------


@pytest.mark.parametrize("case", sorted(DELEGABLE_ROWS), ids=sorted(DELEGABLE_ROWS))
def test_nonpass_facts_lossless_in_persisted_adjudication_and_never_pass(tmp_path, single, case):
    ws = Workspace(tmp_path, single)
    override = DELEGABLE_ROWS[case]
    report = _report(single, row_overrides={0: override})
    report_path = ws.write("report_adj.json", report)
    before = Path(report_path).read_bytes()

    rc, payload = ws.adjudicate(report)

    # Dispatch permission, with a reason_code of None and a recorded seq ...
    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True
    assert payload["reason_code"] is None
    assert payload["seq"] == 1
    assert payload["binding_key"] == ws.binding_key()
    # ... but the adjudication is NOT a pass: the AC is unresolved and says so.
    assert payload["adjudication"]["overall_status"] == "indeterminate"
    assert payload["adjudication"]["blocking"] is True
    assert payload["adjudication"]["errors"] == []
    assert payload["adjudication"]["pr_review_only_nonpass_delegated"] == ["AC1"]
    assert ws.state()["dispatch"] == {"binding_key": ws.binding_key(), "seq": 1}

    stored = ws.stored()
    assert stored["overall_status"] == "indeterminate"
    assert stored["blocking"] is True
    assert stored["rerun_required"] is False
    assert stored["errors"] == []
    assert len(stored["per_ac"]) == 1
    entry = stored["per_ac"][0]
    assert (entry["ac"], entry["command_hash"]) == ("AC1", single.hashes[0])
    assert entry["status"] == "indeterminate" and entry["blocking"] is True
    assert entry["reason_code"] == DELEGATED
    # exit_code / status / fallback_detected survive verbatim in the existing failure_keys field.
    facts = _decoded(entry)
    expected = {
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
    } | override
    assert facts == expected
    # No PASS is manufactured anywhere and the input report is never rewritten.
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(stored)
    assert all(row["status"] != "pass" for row in stored["per_ac"])
    assert Path(report_path).read_bytes() == before

    # Without the opt-in flag the very same failure still fails closed (reason_code, not rc).
    other = Workspace(tmp_path / "no_flag", single)
    rc, payload = other.adjudicate(report, delegate=False)
    _assert_no_dispatch(other, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])


def test_nonpass_facts_lossless_when_mixed_with_ordinary_passing_ac(tmp_path, mixed):
    ws = Workspace(tmp_path, mixed)
    index = _pr_review_only_index(mixed)
    report = _report(mixed, row_overrides={index: {"exit_code": 1, "status": "fail"}})

    rc, payload = ws.adjudicate(report)

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["reason_code"] is None
    assert payload["adjudication"]["pr_review_only_nonpass_delegated"] == ["AC2"]
    stored = ws.stored()
    # Issue declaration order is preserved and the ordinary AC keeps its own resolved PASS.
    assert [entry["command_hash"] for entry in stored["per_ac"]] == mixed.hashes
    assert [entry["ac"] for entry in stored["per_ac"]] == ["AC1", "AC2"]
    assert [entry["reason_code"] for entry in stored["per_ac"]] == [
        "expected_fail_resolved_on_current_head",
        DELEGATED,
    ]
    assert [entry["status"] for entry in stored["per_ac"]] == ["pass", "indeterminate"]
    assert [entry["blocking"] for entry in stored["per_ac"]] == [False, True]
    assert stored["overall_status"] == "indeterminate" and stored["blocking"] is True
    assert _decoded(stored["per_ac"][1])["exit_code"] == 1

    # An ORDINARY failing AC next to the delegated one is never delegated.
    other = Workspace(tmp_path / "ordinary_fail", mixed)
    ordinary_fail = _report(
        mixed, row_overrides={0: {"exit_code": 1, "status": "fail"}, index: {"exit_code": 1, "status": "fail"}}
    )
    rc, payload = other.adjudicate(ordinary_fail)
    assert payload["invoke_pr_reviewer"] is False
    assert payload["seq"] is None
    # persist refuses a result the gate would not open, so nothing is stored for this binding.
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert payload["adjudication"]["blocking"] is True
    assert rc == 1
    assert "dispatch" not in other.state()
    assert other.state().get("vc_adjudication", {}) == {}
    # The gate itself names the reason when handed that adjudication directly.
    direct = mod.adjudicate_vc_result(
        contract_snapshot=mixed.snapshot,
        current_vc_result=mod.adapt_test_verdict_to_current_vc_result(ordinary_fail)[0],
        diff_summary=_diff_summary(mixed),
        allowed_paths=list(ALLOWED_PATHS),
        test_verdict=ordinary_fail,
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
        delegate_pr_review_only_nonpass=True,
    )
    gate = mod.evaluate_step4_vc_gate(
        direct,
        expected_head_sha=mixed.head,
        expected_contract_body_sha256=mixed.body_sha256,
        expected_command_hashes=mixed.hashes,
    )
    # The ordinary AC's own unresolved status demands a re-run, which the gate refuses first.
    assert direct["rerun_required"] is True
    assert [entry["status"] for entry in direct["per_ac"]] == ["indeterminate", "indeterminate"]
    assert gate == {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}


# --- AC2: dispatch permission is not terminal approval -------------------------


def test_terminal_gate_blocks_without_reviewer_approval(tmp_path, single):
    ws = _dispatched_with_failure(tmp_path, single, {"exit_code": 1, "status": "fail"})
    head = single.head

    # Reviewer REQUEST_CHANGES: dispatch was permitted but terminal approval is not reached.
    rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict(head, "REQUEST_CHANGES", ["AC1 failed"]))
    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["route"] != "approved"

    # Reviewer has not judged yet (no verdict file): malformed, nothing approved.
    rc, term = ws.terminal_gate(dispatch_seq=1, verdict_path=str(ws.dir / "no_such_reviewer_verdict.json"))
    assert rc == 2
    assert term["invoke_pr_reviewer"] is False
    assert term["reason_code"] == "reviewer_verdict_malformed"
    assert "route" not in term

    # An undecided / unknown verdict value never approves either.
    for verdict in ("HUMAN_REVIEW_REQUIRED", "", "PENDING"):
        rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict(head, verdict))
        assert rc == 1 and term["route"] != "approved", (verdict, term)

    # An APPROVE for a different dispatch (stale seq) is refused with the dispatch reason_code.
    rc, term = ws.terminal_gate(dispatch_seq=2)
    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "dispatch_seq_mismatch"

    # A reviewer APPROVE reviewed against another head is a stale-head route, not approval.
    rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict("1" * 40))
    assert rc == 1 and term["route"] != "approved"

    # Only the reviewer's APPROVE (the semantic judgement) on the live binding reaches terminal approval.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0
    assert term["route"] == "approved"

    # The persisted adjudication is untouched by all of the above: still not a pass.
    stored = ws.stored()
    assert stored["overall_status"] == "indeterminate" and stored["blocking"] is True


def test_terminal_gate_blocks_without_reviewer_approval_when_failure_is_re_verified_without_delegation(
    tmp_path, single
):
    ws = _dispatched_with_failure(tmp_path, single, {"exit_code": 77, "status": "skip"})
    failing = _report(single, row_overrides={0: {"exit_code": 1, "status": "fail"}})

    # A canonical re-verification of the same binding WITHOUT the opt-in invalidates the
    # delegated adjudication; the earlier reviewer APPROVE (seq 1) cannot be reused.
    rc, payload = ws.adjudicate(failing, delegate=False, tag="reverify")
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == ["pr_review_only_current_execution_not_pass:AC1"]
    assert rc == 1
    assert ws.binding_key() not in ws.state().get("vc_adjudication", {})

    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "vc_gate_blocking"
    assert term["rerun_required"]["verification"] is True


# --- AC3: PASS conversion / forged inputs fail closed --------------------------

# case -> (row override or None, report kwargs, expected errors)
FAIL_CLOSED_CASES: dict[str, dict[str, Any]] = {
    # a failure dressed up as success: contradictory facts are never delegated
    "pass_status_with_non_zero_exit": {
        "row": {"exit_code": 2, "status": "pass"},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "fail_status_with_zero_exit": {
        "row": {"exit_code": 0, "status": "fail", "fallback_detected": False},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    # the producer never emits PASS for a fallback: that expression is contradictory
    "pass_status_with_fallback": {
        "row": {"exit_code": 0, "status": "pass", "fallback_detected": True},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "skip_status_with_zero_exit_and_fallback": {
        "row": {"exit_code": 0, "status": "skip", "fallback_detected": True},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "fail_zero_exit_fallback_with_human_review": {
        "row": {"exit_code": 0, "status": "fail", "fallback_detected": True, "human_review_required": True},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "fail_zero_exit_fallback_with_stop_condition": {
        "row": {"exit_code": 0, "status": "fail", "fallback_detected": True, "stop_condition_triggered": True},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "skip_status_with_zero_exit": {
        "row": {"exit_code": 0, "status": "skip"},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "unknown_status": {
        "row": {"exit_code": 1, "status": "passed"},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "explicit_human_review_signal": {
        "row": {"exit_code": 1, "status": "fail", "human_review_required": True},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    "explicit_stop_condition_signal": {
        "row": {"exit_code": 1, "status": "fail", "stop_condition_triggered": True},
        "errors": ["pr_review_only_current_execution_not_pass:AC1"],
    },
    # report-level PASS next to a failing row (PASS conversion of the aggregate)
    "report_result_pass_over_failing_row": {
        "row": {"exit_code": 1, "status": "fail"},
        "report": {"result": "PASS"},
        "errors": ["pr_review_only_nonpass_report_result_inconsistent"],
    },
    "report_result_pass_over_fallback_row": {
        "row": {"exit_code": 0, "status": "fail", "fallback_detected": True},
        "report": {"result": "PASS"},
        "errors": ["pr_review_only_nonpass_report_result_inconsistent"],
    },
    "report_result_unknown_over_failing_row": {
        "row": {"exit_code": 1, "status": "fail"},
        "report": {"result": "BOGUS"},
        "errors": ["pr_review_only_nonpass_report_result_inconsistent"],
    },
    # forged identifiers: a GitHub trust marker KEY selects the legacy route, which refuses an executed item
    **{
        f"forged_identifier_{marker}": {
            "row": {"exit_code": 1, "status": "fail"},
            "report": {marker: None},
            "errors": ["pr_review_only_current_authorization_mismatch:AC1"],
        }
        for marker in TRUST_MARKER_KEYS
    },
    "forged_identifier_placeholder_value": {
        "row": {"exit_code": 1, "status": "fail"},
        "report": {"workflow_run_id": "TODO-placeholder"},
        "errors": ["pr_review_only_current_authorization_mismatch:AC1"],
    },
    # binding drift is unaffected by the delegation
    "head_drift_reviewed_head": {
        "row": {"exit_code": 1, "status": "fail"},
        "report": {"reviewed_head_sha": "e" * 40},
        "errors": ["pr_review_only_head_binding_mismatch"],
    },
    "body_digest_drift": {
        "row": {"exit_code": 1, "status": "fail"},
        "body_sha256": "sha256:" + "f" * 64,
        "errors": ["pr_review_only_source_body_sha256_mismatch"],
    },
    "issue_number_drift": {
        "row": {"exit_code": 1, "status": "fail"},
        "issue_number": 9999,
        "errors": ["pr_review_only_issue_number_mismatch"],
    },
    "pr_number_drift_report": {
        "row": {"exit_code": 1, "status": "fail"},
        "pr_number": 9999,
        "errors": ["pr_review_only_pr_number_mismatch"],
    },
    "changed_path_outside_allowed_paths": {
        "row": {"exit_code": 1, "status": "fail"},
        "diff": {"changed_paths": ["src/outside.py"]},
        "errors": ["pr_review_only_changed_paths_not_certified"],
    },
    "contract_not_go": {
        "row": {"exit_code": 1, "status": "fail"},
        "snapshot": {"status": "no_go"},
        "errors": ["pr_review_only_contract_not_go"],
    },
    "expected_issue_number_missing": {
        "row": {"exit_code": 1, "status": "fail"},
        "expected_issue": None,
        "errors": ["pr_review_only_expected_issue_number_missing"],
    },
    "expected_pr_number_missing": {
        "row": {"exit_code": 1, "status": "fail"},
        "expected_pr": None,
        "errors": ["pr_review_only_expected_pr_number_missing"],
    },
}


@pytest.mark.parametrize("case", sorted(FAIL_CLOSED_CASES), ids=sorted(FAIL_CLOSED_CASES))
def test_fail_closed_on_pass_conversion_or_forged_input(tmp_path, single, case):
    spec = FAIL_CLOSED_CASES[case]
    ws = Workspace(tmp_path, single)
    report_kwargs = dict(spec.get("report", {}))
    for key in ("body_sha256", "issue_number", "pr_number"):
        if key in spec:
            report_kwargs[key] = spec[key]
    report = _report(single, row_overrides={0: spec["row"]}, **report_kwargs)
    kwargs: dict[str, Any] = {}
    if "diff" in spec:
        kwargs["diff_summary"] = _diff_summary(single, **spec["diff"])
    if "snapshot" in spec:
        kwargs["snapshot"] = {**single.snapshot, **spec["snapshot"]}
    for key in ("expected_issue", "expected_pr"):
        if key in spec:
            kwargs[key] = spec[key]

    rc, payload = ws.adjudicate(report, **kwargs)

    _assert_no_dispatch(ws, rc, payload, spec["errors"])
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(payload)
    # Nothing can be approved from this state either (no dispatch was ever recorded).
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "dispatch_seq_mismatch"


def test_fail_closed_on_pass_conversion_or_forged_skip_envelope_echo_covering_a_failure(single):
    # A baseline skip envelope echoed on the CURRENT side is not execution evidence: it cannot be used
    # to cover a failure, even with the opt-in flag on the independent route.
    report = _report(single, row_overrides={0: {"exit_code": 1, "status": "fail"}})
    echo = {
        "schema": "baseline_vc_preflight/v1",
        "issue": ISSUE_NUMBER,
        "generated_at": GENERATED_AT,
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": single.body_sha256},
        "head_sha": single.head,
        "reviewed_head_sha": single.head,
        "results": copy.deepcopy(single.producer["results"]),
    }
    result = mod.adjudicate_vc_result(
        contract_snapshot=single.snapshot,
        current_vc_result=echo,
        diff_summary=_diff_summary(single),
        allowed_paths=list(ALLOWED_PATHS),
        test_verdict=report,
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
        delegate_pr_review_only_nonpass=True,
    )
    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["pr_review_only_independent_requires_executed_item:AC1"]
    assert result["per_ac"] == []
    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=single.head,
        expected_contract_body_sha256=single.body_sha256,
        expected_command_hashes=single.hashes,
    )
    assert gate == {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}


FALLBACK_REASON = "pr_review_only_fallback_detected"


def test_fail_closed_on_pass_conversion_or_forged_ordinary_ac_fallback_next_to_delegated_row(tmp_path, mixed):
    # An ORDINARY AC carrying fallback_detected: true must never be turned into PASS (and so open a reviewer
    # dispatch) merely because a DIFFERENT pr_review_only row is delegated. Row-level, not report-level.
    index = _pr_review_only_index(mixed)
    report = _report(
        mixed,
        row_overrides={0: {"fallback_detected": True}, index: {"exit_code": 1, "status": "fail"}},
    )
    ws = Workspace(tmp_path / "delegated", mixed)

    rc, payload = ws.adjudicate(report)

    _assert_no_dispatch(ws, rc, payload, [FALLBACK_REASON])
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(payload)
    assert "expected_fail_resolved_on_current_head" not in json.dumps(payload)
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "dispatch_seq_mismatch"

    # The same ordinary-row fallback is refused with the SAME reason_code when nothing is delegated
    # (the pr_review_only row passes), with and without the opt-in flag: delegation changes nothing here.
    clean_report = _report(mixed, row_overrides={0: {"fallback_detected": True}}, result="PASS")
    for delegate in (False, True):
        other = Workspace(tmp_path / f"compare_delegate_{delegate}", mixed)
        rc, payload = other.adjudicate(clean_report, delegate=delegate)
        _assert_no_dispatch(other, rc, payload, [FALLBACK_REASON])

    # A delegated row that itself fails AND carries fallback does not excuse an ordinary row's fallback either.
    both = _report(
        mixed,
        row_overrides={
            0: {"fallback_detected": True},
            index: {"exit_code": 1, "status": "fail", "fallback_detected": True},
        },
    )
    other = Workspace(tmp_path / "delegated_with_fallback", mixed)
    rc, payload = other.adjudicate(both)
    _assert_no_dispatch(other, rc, payload, [FALLBACK_REASON])


def test_fail_closed_on_pass_conversion_or_forged_ordinary_fallback_via_direct_call(mixed):
    index = _pr_review_only_index(mixed)
    report = _report(
        mixed,
        row_overrides={0: {"fallback_detected": True}, index: {"exit_code": 1, "status": "fail"}},
    )
    result = mod.adjudicate_vc_result(
        contract_snapshot=mixed.snapshot,
        current_vc_result=mod.adapt_test_verdict_to_current_vc_result(report)[0],
        diff_summary=_diff_summary(mixed),
        allowed_paths=list(ALLOWED_PATHS),
        test_verdict=report,
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
        delegate_pr_review_only_nonpass=True,
    )
    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == [FALLBACK_REASON]
    assert result["per_ac"] == []


def test_fail_closed_on_pass_conversion_or_forged_delegation_is_off_by_default_for_direct_calls(single):
    # The function-level default must stay fail-closed: no opt-in, no delegation.
    report = _report(single, row_overrides={0: {"exit_code": 1, "status": "fail"}})
    result = mod.adjudicate_vc_result(
        contract_snapshot=single.snapshot,
        current_vc_result=mod.adapt_test_verdict_to_current_vc_result(report)[0],
        diff_summary=_diff_summary(single),
        allowed_paths=list(ALLOWED_PATHS),
        test_verdict=report,
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )
    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["pr_review_only_current_execution_not_pass:AC1"]
    assert result["per_ac"] == []


def test_fail_closed_on_pass_conversion_or_forged_delegated_row_only_fallback_stays_delegable(tmp_path, mixed):
    # Existing behaviour is unchanged: when ONLY the delegated pr_review_only row carries fallback_detected,
    # the ordinary AC is clean and the reviewer dispatch is still permitted (and still not a pass).
    index = _pr_review_only_index(mixed)
    report = _report(mixed, row_overrides={index: {"exit_code": 0, "status": "fail", "fallback_detected": True}})
    ws = Workspace(tmp_path, mixed)

    rc, payload = ws.adjudicate(report)

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["reason_code"] is None
    assert payload["adjudication"]["pr_review_only_nonpass_delegated"] == ["AC2"]
    stored = ws.stored()
    assert [entry["status"] for entry in stored["per_ac"]] == ["pass", "indeterminate"]
    assert [entry["reason_code"] for entry in stored["per_ac"]] == [
        "expected_fail_resolved_on_current_head",
        DELEGATED,
    ]
    assert _decoded(stored["per_ac"][1])["fallback_detected"] is True


def _forge(entry: dict[str, Any], how: str) -> None:
    facts_pass = {
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
    }
    if how == "facts_rewritten_to_pass":
        entry["failure_keys"] = mod._encode_nonpass_facts(facts_pass)
    elif how == "status_pass_with_delegated_reason":
        entry["status"] = "pass"
        entry["blocking"] = False
    elif how == "blocking_false_with_delegated_reason":
        entry["blocking"] = False
    elif how == "facts_dropped":
        entry["failure_keys"] = []
    elif how == "facts_contradictory":
        entry["failure_keys"] = mod._encode_nonpass_facts({**facts_pass, "exit_code": 2})
    elif how == "foreign_fact_kind":
        entry["failure_keys"] = [{**row, "kind": "pytest_nodeid"} for row in entry["failure_keys"]]
    else:  # pragma: no cover - guards the parametrization itself
        raise AssertionError(how)


@pytest.mark.parametrize(
    "how",
    [
        "facts_rewritten_to_pass",
        "status_pass_with_delegated_reason",
        "blocking_false_with_delegated_reason",
        "facts_dropped",
        "facts_contradictory",
        "foreign_fact_kind",
    ],
)
def test_fail_closed_on_pass_conversion_or_forged_persisted_adjudication(tmp_path, single, how):
    ws = _dispatched_with_failure(tmp_path, single, {"exit_code": 1, "status": "fail"})
    # Sanity (normal axis): the untouched persisted adjudication opens the gate and approves.
    rc, gate = ws.step4_gate()
    assert rc == 0 and gate["invoke_pr_reviewer"] is True and gate["reason_code"] is None

    state = ws.state()
    _forge(state["vc_adjudication"][ws.binding_key()]["per_ac"][0], how)
    ws.loop_state.write_text(json.dumps(state), encoding="utf-8")

    rc, gate = ws.step4_gate()
    assert rc == 1
    assert gate["invoke_pr_reviewer"] is False
    assert gate["reason_code"] in {"adjudication_ac_not_resolved", "adjudication_blocking_true"}
    # Even a reviewer APPROVE on the live binding cannot reach terminal approval from a forged state.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "vc_gate_blocking"


def test_fail_closed_on_pass_conversion_or_forged_overall_marked_non_blocking(tmp_path, single):
    ws = _dispatched_with_failure(tmp_path, single, {"exit_code": 1, "status": "fail"})
    state = ws.state()
    stored = state["vc_adjudication"][ws.binding_key()]
    stored["blocking"] = False
    stored["overall_status"] = "pass"
    ws.loop_state.write_text(json.dumps(state), encoding="utf-8")

    rc, gate = ws.step4_gate()
    assert rc == 1
    assert gate == {"invoke_pr_reviewer": False, "reason_code": "adjudication_ac_not_resolved", "reused": True}
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] == "continue_loop" and term["reason_code"] == "vc_gate_blocking"


# --- AC4: reason_code / invoke_pr_reviewer / route, never a bare return code ----

# (scenario, report row override | None for a clean PASS, delegate flag)
#   -> (step4 rc, invoke_pr_reviewer, reason_code, adjudication errors, terminal route for an APPROVE)
ROUTE_MATRIX: dict[str, tuple[str, dict[str, Any] | None, bool, int, bool, str | None, list[str], str]] = {
    "clean_pass_opens_gate_and_approves": ("single", None, True, 0, True, None, [], "approved"),
    "failing_item_delegated_dispatches_but_awaits_terminal_gate": (
        "single",
        {"exit_code": 1, "status": "fail"},
        True,
        0,
        True,
        None,
        [],
        "approved",
    ),
    "failing_item_without_opt_in_is_not_dispatched": (
        "single",
        {"exit_code": 1, "status": "fail"},
        False,
        1,
        False,
        "adjudication_missing_or_malformed",
        ["pr_review_only_current_execution_not_pass:AC1"],
        "continue_loop",
    ),
    "skip_without_opt_in_is_not_dispatched": (
        "single",
        {"exit_code": 77, "status": "skip"},
        False,
        1,
        False,
        "adjudication_missing_or_malformed",
        ["pr_review_only_current_execution_not_pass:AC1"],
        "continue_loop",
    ),
    "incoherent_facts_are_not_dispatched_even_with_opt_in": (
        "single",
        {"exit_code": 3, "status": "pass"},
        True,
        1,
        False,
        "adjudication_missing_or_malformed",
        ["pr_review_only_current_execution_not_pass:AC1"],
        "continue_loop",
    ),
}


@pytest.mark.parametrize("case", sorted(ROUTE_MATRIX), ids=sorted(ROUTE_MATRIX))
def test_asserts_reason_code_and_route_across_step4_and_terminal_gate(tmp_path, request, case):
    scenario_name, override, delegate, _rc, invoke, reason_code, errors, route = ROUTE_MATRIX[case]
    scenario: Scenario = request.getfixturevalue(scenario_name)
    ws = Workspace(tmp_path, scenario)
    report = _report(scenario, row_overrides={0: override or {}}, result="FAIL" if override else "PASS")

    rc, payload = ws.adjudicate(report, delegate=delegate)

    assert payload["invoke_pr_reviewer"] is invoke
    assert payload["reason_code"] == reason_code
    assert payload["adjudication"]["errors"] == errors
    assert rc == (0 if invoke else 1)
    if invoke:
        assert payload["seq"] == 1
        assert ws.state()["dispatch"] == {"binding_key": ws.binding_key(), "seq": 1}
    else:
        assert payload["seq"] is None
        assert "dispatch" not in ws.state()

    # The terminal route for an APPROVE on the live binding, in a separate process.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert term["route"] == route
    assert rc == (0 if route == "approved" else 1)
    if not invoke:
        assert term["reason_code"] == "dispatch_seq_mismatch"
    # A REQUEST_CHANGES on the same dispatch never approves, whatever Step 4 allowed.
    rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict(scenario.head, "REQUEST_CHANGES", ["x"]))
    assert rc == 1
    assert term["route"] == "continue_loop"


# --- Issue #2916 review fix: runtime_only PASS + delegated pr_review_only non-pass ---

RT_PASS_ROW: dict[str, Any] = {}  # default _row is an executed PASS
PR_FAIL_ROWS: dict[str, dict[str, Any]] = {
    "fail_exit_1": {"exit_code": 1, "status": "fail"},
    "fail_exit_0_with_fallback": {"exit_code": 0, "status": "fail", "fallback_detected": True},
}


@pytest.mark.parametrize("report_result", ["FAIL", "PARTIAL"])
@pytest.mark.parametrize("case", sorted(PR_FAIL_ROWS), ids=sorted(PR_FAIL_ROWS))
def test_nonpass_facts_lossless_runtime_only_pass_mixed_with_delegated_pr_review_only_fail(
    tmp_path, runtime_mixed, case, report_result
):
    ws = Workspace(tmp_path, runtime_mixed)
    report = _report(runtime_mixed, row_overrides={1: PR_FAIL_ROWS[case]}, result=report_result)
    rc, payload = ws.adjudicate(report)
    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["reason_code"] is None and payload["seq"] == 1
    assert payload["adjudication"]["pr_review_only_nonpass_delegated"] == ["AC2"]
    stored = ws.stored()
    assert stored["overall_status"] == "indeterminate" and stored["blocking"] is True
    by_ac = {entry["ac"]: entry for entry in stored["per_ac"]}
    # runtime_only keeps its own executed PASS; only the pr_review_only AC is delegated (never PASS).
    assert by_ac["AC1"]["reason_code"] == "runtime_only_current_head_binding_pass"
    assert by_ac["AC2"]["reason_code"] == DELEGATED and by_ac["AC2"]["status"] == "indeterminate"
    assert _decoded(by_ac["AC2"])["status"] == "fail"
    # Reviewer dispatch is not AC achievement: no APPROVE -> no terminal approval.
    rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict(runtime_mixed.head, "REQUEST_CHANGES", ["x"]))
    assert rc == 1 and term["route"] != "approved"
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0 and term["route"] == "approved"

    # Without the opt-in flag the legacy fail-closed behaviour is unchanged.
    other = Workspace(tmp_path / "no_flag", runtime_mixed)
    rc, payload = other.adjudicate(report, delegate=False)
    _assert_no_dispatch(other, rc, payload, ["pr_review_only_current_execution_not_pass:AC2"])


def test_fail_closed_on_pass_conversion_or_forged_sibling_entry_marked_blocking_next_to_delegated(
    tmp_path, runtime_mixed
):
    """A delegated entry opens the gate only for itself: a sibling resolved entry that
    persisted as ``blocking: true`` (e.g. tampered state) never rides along with it."""
    ws = Workspace(tmp_path, runtime_mixed)
    report = _report(runtime_mixed, row_overrides={1: PR_FAIL_ROWS["fail_exit_1"]}, result="FAIL")
    rc, payload = ws.adjudicate(report)
    assert rc == 0 and payload["invoke_pr_reviewer"] is True and payload["seq"] == 1, payload
    # Sanity (normal axis): the untouched persisted adjudication opens the gate.
    rc, gate = ws.step4_gate()
    assert rc == 0 and gate["invoke_pr_reviewer"] is True

    state = ws.state()
    sibling = next(e for e in state["vc_adjudication"][ws.binding_key()]["per_ac"] if e["ac"] == "AC1")
    assert sibling["reason_code"] != DELEGATED and sibling["status"] == "pass"
    sibling["blocking"] = True
    ws.loop_state.write_text(json.dumps(state), encoding="utf-8")

    rc, gate = ws.step4_gate()
    assert rc == 1
    assert gate["invoke_pr_reviewer"] is False
    assert gate["reason_code"] == "adjudication_blocking_true"
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] == "continue_loop" and term["reason_code"] == "vc_gate_blocking"


RUNTIME_ROW_REFUSED: dict[str, dict[str, Any]] = {
    "runtime_only_fail": {"exit_code": 1, "status": "fail"},
    "runtime_only_skip": {"exit_code": 77, "status": "skip"},
    "runtime_only_pass_with_fallback": {"fallback_detected": True},
    "runtime_only_fail_zero_exit_with_fallback": {"exit_code": 0, "status": "fail", "fallback_detected": True},
}


@pytest.mark.parametrize("case", sorted(RUNTIME_ROW_REFUSED), ids=sorted(RUNTIME_ROW_REFUSED))
def test_fail_closed_on_pass_conversion_or_forged_runtime_only_nonpass_next_to_delegated_row(
    tmp_path, runtime_mixed, case
):
    ws = Workspace(tmp_path, runtime_mixed)
    report = _report(
        runtime_mixed,
        row_overrides={0: RUNTIME_ROW_REFUSED[case], 1: {"exit_code": 1, "status": "fail"}},
    )
    rc, payload = ws.adjudicate(report)
    # runtime_only is never delegable: a delegated pr_review_only row cannot cover it.
    _assert_no_dispatch(ws, rc, payload, ["runtime_only_current_execution_not_pass:AC1"])


def test_fail_closed_on_pass_conversion_or_forged_runtime_mixed_bindings(tmp_path, runtime_mixed):
    failing = {1: {"exit_code": 1, "status": "fail"}}
    drifts: dict[str, tuple[dict[str, Any], dict[str, Any], list[str]]] = {
        "stale_reviewed_head": ({"reviewed_head_sha": "e" * 40}, {}, ["pr_review_only_head_binding_mismatch"]),
        "stale_body_digest": (
            {"body_sha256": "sha256:" + "f" * 64},
            {},
            ["pr_review_only_source_body_sha256_mismatch"],
        ),
        "changed_path_outside_allowed": (
            {},
            {"diff_summary": _diff_summary(runtime_mixed, changed_paths=["src/outside.py"])},
            ["pr_review_only_changed_paths_not_certified"],
        ),
    }
    for name, (report_kwargs, adjudicate_kwargs, errors) in drifts.items():
        ws = Workspace(tmp_path / name, runtime_mixed)
        rc, payload = ws.adjudicate(_report(runtime_mixed, row_overrides=failing, **report_kwargs), **adjudicate_kwargs)
        _assert_no_dispatch(ws, rc, payload, errors)


def test_fail_closed_on_pass_conversion_or_forged_report_pass_over_runtime_mixed_failure(tmp_path, runtime_mixed):
    ws = Workspace(tmp_path, runtime_mixed)
    report = _report(runtime_mixed, row_overrides={1: {"exit_code": 1, "status": "fail"}}, result="PASS")
    rc, payload = ws.adjudicate(report)
    _assert_no_dispatch(ws, rc, payload, ["pr_review_only_nonpass_report_result_inconsistent"])


@pytest.mark.parametrize(
    "forged",
    [
        {"status": "pass"},
        {"fallback_detected": False},
        {"exit_code": 3, "status": "pass"},
    ],
    ids=["status_pass", "fallback_dropped", "pass_nonzero_exit"],
)
def test_fail_closed_on_pass_conversion_or_forged_persisted_fallback_fail_facts_tampered(tmp_path, single, forged):
    ws = _dispatched_with_failure(
        tmp_path, single, {"exit_code": 0, "status": "fail", "fallback_detected": True}
    )
    rc, gate = ws.step4_gate()
    assert rc == 0 and gate["invoke_pr_reviewer"] is True

    state = ws.state()
    entry = state["vc_adjudication"][ws.binding_key()]["per_ac"][0]
    facts = _decoded(entry) | forged
    entry["failure_keys"] = [
        {"kind": "pr_review_only_current_execution_fact", "key": f"{name}={json.dumps(value)}"}
        for name, value in facts.items()
    ]
    ws.loop_state.write_text(json.dumps(state), encoding="utf-8")

    rc, gate = ws.step4_gate()
    assert rc == 1 and gate["invoke_pr_reviewer"] is False
    assert gate["reason_code"] in {"adjudication_ac_not_resolved", "adjudication_blocking_true"}
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] == "continue_loop" and term["reason_code"] == "vc_gate_blocking"
