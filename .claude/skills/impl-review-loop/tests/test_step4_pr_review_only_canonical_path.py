"""Issue #2912: pr_review_only VC through the canonical step4-adjudicate -> step5-terminal-gate path.

These tests drive the PRODUCTION entrypoints only: the real
``adjudicate_vc_result.py`` CLI (``step4-adjudicate`` / ``step5-terminal-gate`` /
``extract-vc-metadata``) through subprocesses, the real
``baseline_vc_preflight.py`` producer for the baseline authority, and the module
function ``adjudicate_vc_result()`` in-process for the layer that the CLI cannot
observe. Nothing in the adjudicator / persist / gate chain is mocked, no CI
identifier is filled into a fixture, and no skip / xfail marker is used.

Four concepts stay separate in this file:

1. baseline scope authorization (the producer skip envelope in the contract snapshot),
2. current-head execution facts (the independent test-runner report adapted per command),
3. reviewer semantic judgement (a reviewer verdict file), and
4. terminal approval (only ``step5-terminal-gate`` exit 0).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
SCRIPT_PATH = SKILL_DIR / "scripts" / "adjudicate_vc_result.py"
PRODUCER_PATH = ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
STEP2_DOC = SKILL_DIR / "steps" / "step-2-verification.md"
STEP4_DOC = SKILL_DIR / "steps" / "step-4-pr-review.md"
STEP5_DOC = SKILL_DIR / "steps" / "step-5-feedback-and-termination.md"

_spec = importlib.util.spec_from_file_location("adjudicate_vc_result_step4_pr_review_only_canonical_path", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


ISSUE_NUMBER = 2912
PR_NUMBER = 2930
GENERATED_AT = "2026-10-04T12:00:00Z"
CHANGED_PATHS = ["implemented.txt"]
ALLOWED_PATHS = ["tracked.txt", "implemented.txt", "implemented2.txt"]

# GitHub-specific trust markers. Their mere PRESENCE (even as null) selects the legacy route.
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

BODY_MULTI = """## Allowed Paths
- tracked.txt
- implemented.txt

## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 1

# AC1
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 2

# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 3

# AC2, AC3
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 4
```
"""

BODY_MIXED = """## Allowed Paths
- tracked.txt
- implemented.txt
- implemented2.txt

## Verification Commands

```bash
# AC1
$ test -f implemented.txt

# AC2
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt

# AC3
# preflight-scope: runtime_only
$ rg -q fixture tracked.txt --max-count 1

# AC4
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 2

# AC4
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 3

# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 4

# AC5, AC6
# preflight-scope: pr_review_only
$ rg -q fixture tracked.txt --max-count 5

# AC7
$ test -f implemented2.txt
```
"""

BODY_LEGACY_MIXED = """## Allowed Paths
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


# --- real producer / real metadata extractor ---------------------------------


@dataclass
class Scenario:
    """A baseline authority produced by the REAL baseline_vc_preflight.py."""

    name: str
    head: str
    body_sha256: str
    producer: dict[str, Any]
    snapshot: dict[str, Any]
    commands: list[dict[str, Any]]
    hashes: list[str]
    kinds: list[str] = field(default_factory=list)
    # The git repo the baseline was produced in (Issue #3020: executed fixtures run here, at ``head``).
    repo: Path | None = None

    def indices(self, kind: str) -> list[int]:
        return [index for index, value in enumerate(self.kinds) if value == kind]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _init_repo(repo: Path, extra_files: dict[str, str] | None = None) -> str:
    """Create a git repo with ``tracked.txt`` plus ``extra_files`` committed; return the HEAD SHA."""
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    files = {"tracked.txt": "fixture\n", **(extra_files or {})}
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _build_scenario(
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    body: str,
    *,
    repo_files: dict[str, str] | None = None,
) -> Scenario:
    base = tmp_path_factory.mktemp(f"scenario_{name}")
    repo = base / "repo"
    head = _init_repo(repo, repo_files)
    # The body lives OUTSIDE the repo so it does not dirty the tree under test.
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
    assert metadata_run.returncode == 0, metadata_run.stdout + metadata_run.stderr
    metadata = json.loads(metadata_run.stdout)
    commands = metadata["commands"]
    hashes = metadata["command_hashes"]
    # The parse-only metadata and the real producer agree on (ac, hash) and on order.
    assert [(row["ac"], row["command_hash"]) for row in producer["results"]] == [
        (row["ac"], row["command_hash"]) for row in commands
    ]
    assert hashes == [row["command_hash"] for row in commands]

    kinds = []
    for row in producer["results"]:
        if row["scope_class"] == "pr_review_only":
            kinds.append("pr_review_only")
        elif row["scope_class"] == "runtime_only":
            kinds.append("runtime_only")
        else:
            kinds.append("ordinary")
    snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": producer["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": producer["results"]}},
    }
    return Scenario(
        name=name,
        head=head,
        body_sha256=producer["source"]["body_sha256"],
        producer=producer,
        snapshot=snapshot,
        commands=commands,
        hashes=hashes,
        kinds=kinds,
        repo=repo,
    )


@pytest.fixture(scope="module")
def single(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "single", BODY_SINGLE)
    assert scenario.kinds == ["pr_review_only"]
    return scenario


@pytest.fixture(scope="module")
def multi(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "multi", BODY_MULTI)
    assert scenario.kinds == ["pr_review_only"] * 4
    assert [row["ac"] for row in scenario.commands] == ["AC1", "AC1", "AC_UNKNOWN", "AC2,AC3"]
    return scenario


@pytest.fixture(scope="module")
def mixed(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "mixed", BODY_MIXED)
    assert scenario.kinds == [
        "ordinary",
        "pr_review_only",
        "runtime_only",
        "pr_review_only",
        "pr_review_only",
        "pr_review_only",
        "pr_review_only",
        "ordinary",
    ]
    assert [row["ac"] for row in scenario.commands] == [
        "AC1",
        "AC2",
        "AC3",
        "AC4",
        "AC4",
        "AC_UNKNOWN",
        "AC5,AC6",
        "AC7",
    ]
    return scenario


@pytest.fixture(scope="module")
def legacy_mixed(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "legacy_mixed", BODY_LEGACY_MIXED)
    assert scenario.kinds == ["ordinary", "pr_review_only"]
    return scenario


# --- test-runner report builders (independent report: no trust marker) --------


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
    drop: tuple[int, ...] = (),
    extra_rows: list[dict[str, Any]] | None = None,
    descriptive: bool = True,
    head: str | None = None,
    body_sha256: str | None = None,
    issue_number: Any = ISSUE_NUMBER,
    pr_number: Any = PR_NUMBER,
    result: str = "PASS",
    **top_level: Any,
) -> dict[str, Any]:
    """The independent producer report: execution facts only.

    ``descriptive`` adds the descriptive-only ``producer_kind`` / ``repository`` /
    ``run_id`` / ``run_url`` (and count) fields, which are allowed but never required
    and never a trust marker.
    """
    report_head = head or scenario.head
    rows = [
        _row(command, **((row_overrides or {}).get(index, {})))
        for index, command in enumerate(scenario.commands)
        if index not in drop
    ]
    rows.extend(extra_rows or [])
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
        "runtime_ac_results": rows,
    }
    if descriptive:
        report.update(
            {
                "producer_kind": "test-runner",
                "repository": "squne121/agent-platform",
                "run_id": "local-independent-run-2912",
                "run_url": "https://example.invalid/local/independent-run-2912",
                "verification_commands_pass": len(rows),
                "verification_commands_fail": 0,
                "verification_skipped_count": 0,
            }
        )
    report.update(top_level)
    return report


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


def _live_mergeability(head: str) -> dict[str, Any]:
    return {"head_sha": head, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"}


class Workspace:
    """Caller-side files a root would pass to the real CLI (no mocks)."""

    def __init__(self, tmp_path: Path, scenario: Scenario) -> None:
        self.dir = tmp_path
        self.scenario = scenario
        self.loop_state = tmp_path / "loop_state.json"

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

    def binding_args(
        self, *, head: str | None = None, body: str | None = None, hashes: list[str] | None = None, tag: str = "b"
    ) -> list[str]:
        return [
            "--loop-state-file",
            str(self.loop_state),
            "--expected-head-sha",
            head or self.scenario.head,
            "--expected-contract-body-sha256",
            body if body is not None else self.scenario.body_sha256,
            "--expected-command-hashes-file",
            self.write(f"hashes_{tag}.json", hashes if hashes is not None else self.scenario.hashes),
        ]

    def adjudicate(
        self,
        report: Any = None,
        *,
        report_path: str | None = None,
        diff_summary: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        allowed: list[str] | None = None,
        expected_issue: int | None = ISSUE_NUMBER,
        expected_pr: int | None = PR_NUMBER,
        extra_args: tuple[str, ...] = (),
        hashes: list[str] | None = None,
        tag: str = "adj",
    ) -> tuple[int, dict[str, Any]]:
        """Run `step4-adjudicate` (the only reviewer-dispatch entrance)."""
        scenario = self.scenario
        if report_path is None:
            report_path = self.write(f"report_{tag}.json", report if report is not None else _report(scenario))
        argv = [
            "step4-adjudicate",
            *self.binding_args(hashes=hashes, tag=tag),
            "--test-verdict-file",
            report_path,
            "--contract-snapshot-file",
            self.write(f"snapshot_{tag}.json", snapshot if snapshot is not None else scenario.snapshot),
            "--diff-summary-file",
            self.write(f"diff_{tag}.json", diff_summary if diff_summary is not None else _diff_summary(scenario)),
            "--allowed-paths-file",
            self.write(f"allowed_{tag}.json", allowed if allowed is not None else ALLOWED_PATHS),
            *extra_args,
        ]
        if expected_issue is not None:
            argv += ["--expected-issue-number", str(expected_issue)]
        if expected_pr is not None:
            argv += ["--expected-pr-number", str(expected_pr)]
        return self.run(argv)

    def terminal_gate(
        self,
        *,
        dispatch_seq: int,
        head: str | None = None,
        live_head: str | None = None,
        body: str | None = None,
        hashes: list[str] | None = None,
        verdict: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        head = head or self.scenario.head
        argv = [
            "step5-terminal-gate",
            *self.binding_args(head=head, body=body, hashes=hashes, tag="term"),
            "--reviewer-verdict-file",
            self.write("reviewer_verdict.json", verdict if verdict is not None else _reviewer_verdict(head)),
            "--live-mergeability-file",
            self.write("live_mergeability.json", _live_mergeability(live_head or head)),
            "--dispatch-seq",
            str(dispatch_seq),
        ]
        return self.run(argv)

    def binding_key(self) -> str:
        return mod.step4_binding_key(
            head_sha=self.scenario.head,
            contract_body_sha256=self.scenario.body_sha256,
            command_hashes=self.scenario.hashes,
        )


def _assert_fail_closed_no_dispatch(ws: Workspace, rc: int, payload: dict[str, Any], errors: list[str]) -> None:
    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False, payload
    assert payload["seq"] is None, payload
    assert payload["reason_code"] == "adjudication_missing_or_malformed", payload
    assert payload["adjudication"]["errors"] == errors, payload
    assert payload["adjudication"]["blocking"] is True
    state = ws.state()
    assert "dispatch" not in state
    assert state.get("vc_adjudication", {}) == {}


def _approve_and_assert(ws: Workspace, payload: dict[str, Any]) -> None:
    """Second process: step5-terminal-gate approves from the saved dispatch seq."""
    seq = payload["seq"]
    assert seq == 1
    rc, term = ws.terminal_gate(dispatch_seq=seq)
    assert rc == 0, term
    assert term["route"] == "approved", term


# --- AC1 ---------------------------------------------------------------------


@pytest.mark.parametrize("descriptive", [True, False], ids=["with_descriptive_fields", "minimal_report"])
def test_canonical_independent_path_reaches_terminal_approval(tmp_path, single, descriptive):
    ws = Workspace(tmp_path, single)
    report = _report(single, descriptive=descriptive)
    # The independent report carries no GitHub-specific trust marker at all.
    assert not set(TRUST_MARKER_KEYS) & set(report)
    baseline_item = single.snapshot["checks"]["vc_preflight"]["classifications"][0]
    assert baseline_item["category"] == "preflight_scope_pr_review_only"

    rc, payload = ws.adjudicate(report)

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True
    assert payload["reason_code"] is None
    assert payload["seq"] == 1
    assert payload["binding_key"] == ws.binding_key()
    assert payload["adjudication"]["blocking"] is False
    assert payload["adjudication"]["errors"] == []
    state = ws.state()
    assert state["dispatch"] == {"binding_key": ws.binding_key(), "seq": 1}
    stored = state["vc_adjudication"][ws.binding_key()]
    assert stored["overall_status"] == "pass" and stored["blocking"] is False
    assert [entry["reason_code"] for entry in stored["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]
    assert [entry["command_hash"] for entry in stored["per_ac"]] == single.hashes

    # A reviewer APPROVE plus the live binding is what approves, in another process.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0, term
    assert term["route"] == "approved"

    # step4-adjudicate exit 0 alone is only a dispatch permission: a reviewer
    # REQUEST_CHANGES on the very same dispatch never becomes terminal approval.
    rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict(single.head, "REQUEST_CHANGES", ["fix"]))
    assert rc == 1 and term["route"] != "approved", term


# --- AC2 ---------------------------------------------------------------------


def _per_ac_reason_codes(kinds: list[str], scenario: Scenario) -> list[str]:
    codes = []
    for kind, command in zip(kinds, scenario.commands):
        if kind == "pr_review_only":
            codes.append("pr_review_only_runtime_evidence_pass")
        elif kind == "runtime_only":
            codes.append("runtime_only_current_head_binding_pass")
        else:
            codes.append("expected_fail_resolved_on_current_head")
    return codes


@pytest.mark.parametrize("scenario_name", ["single", "multi", "mixed"])
def test_scope_mixture_and_order_are_preserved(tmp_path, request, scenario_name):
    scenario: Scenario = request.getfixturevalue(scenario_name)
    ws = Workspace(tmp_path, scenario)
    changed = ["implemented.txt", "implemented2.txt"] if scenario_name == "mixed" else list(CHANGED_PATHS)

    rc, payload = ws.adjudicate(_report(scenario), diff_summary=_diff_summary(scenario, changed_paths=changed))

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["reason_code"] is None
    assert payload["adjudication"]["errors"] == []
    stored = ws.state()["vc_adjudication"][ws.binding_key()]
    # per_ac keeps every resolved entry at the Issue declaration position, so its
    # command_hash column equals --expected-command-hashes-file, order included.
    assert [entry["command_hash"] for entry in stored["per_ac"]] == scenario.hashes
    assert [entry["ac"] for entry in stored["per_ac"]] == [row["ac"] for row in scenario.commands]
    assert [entry["reason_code"] for entry in stored["per_ac"]] == _per_ac_reason_codes(scenario.kinds, scenario)
    assert all(entry["status"] == "pass" and entry["blocking"] is False for entry in stored["per_ac"])
    _approve_and_assert(ws, payload)


def test_scope_mixture_and_order_are_preserved_label_echo_and_hash_reorder(tmp_path, multi, mixed):
    # Literal AC_UNKNOWN and comma-joined labels are echoed verbatim; renaming or
    # range-compressing a label breaks the (ac, command_hash) mapping fail-closed.
    ws = Workspace(tmp_path / "label", multi)
    ws.dir.mkdir()
    renamed = _report(multi)
    renamed["runtime_ac_results"][2]["ac"] = "AC3"
    rc, payload = ws.adjudicate(renamed)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == ["pr_review_only_coverage_mismatch"]

    # The persisted per_ac order is the Issue declaration order: the same set of expected
    # hashes in a different order is a different binding, so persist refuses to store the
    # adjudication and no dispatch is recorded.
    ws = Workspace(tmp_path / "order", mixed)
    ws.dir.mkdir()
    changed = _diff_summary(mixed, changed_paths=["implemented.txt", "implemented2.txt"])
    rc, payload = ws.adjudicate(_report(mixed), diff_summary=changed, hashes=list(reversed(mixed.hashes)))
    assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == []
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert "dispatch" not in ws.state() and ws.state().get("vc_adjudication", {}) == {}


# --- AC3 (step4-adjudicate CLI layer) ----------------------------------------


def _drop_pr_review_only_row(scenario: Scenario) -> dict[str, Any]:
    return _report(scenario, drop=(scenario.indices("pr_review_only")[0],))


STEP4_CASES: dict[str, dict[str, Any]] = {
    "report_not_found": {"scenario": "single", "errors": "input_file_not_found"},
    "adapt_failed_schema": {
        "scenario": "single",
        "errors": ["adapt_failed", "unsupported_source_schema:'NOT_A_TEST_VERDICT'"],
    },
    "pr_review_only_report_missing_row": {"scenario": "single", "errors": ["pr_review_only_coverage_mismatch"]},
    "duplicate_ac_command_hash": {"scenario": "single", "errors": ["duplicate_current_ac_command_hash:AC1"]},
    "unknown_extra_ac_row": {"scenario": "single", "errors": ["baseline_current_mapping_mismatch"]},
    "ordinary_row_missing": {"scenario": "mixed", "errors": ["baseline_current_mapping_mismatch"]},
    "unknown_ac_replaces_ordinary_row": {"scenario": "mixed", "errors": ["baseline_current_mapping_mismatch"]},
    "head_drift_diff_head": {"scenario": "single", "errors": ["pr_review_only_head_binding_mismatch"]},
    "head_drift_reviewed_head": {"scenario": "single", "errors": ["pr_review_only_head_binding_mismatch"]},
    "body_digest_drift": {"scenario": "single", "errors": ["pr_review_only_source_body_sha256_mismatch"]},
    "issue_number_drift": {"scenario": "single", "errors": ["pr_review_only_issue_number_mismatch"]},
    "pr_number_drift_diff_summary": {"scenario": "single", "errors": ["pr_review_only_pr_number_mismatch"]},
    "pr_number_drift_report": {"scenario": "single", "errors": ["pr_review_only_pr_number_mismatch"]},
    "expected_issue_number_missing": {"scenario": "single", "errors": ["pr_review_only_expected_issue_number_missing"]},
    "expected_pr_number_missing": {"scenario": "single", "errors": ["pr_review_only_expected_pr_number_missing"]},
    "changed_path_outside_allowed_paths": {
        "scenario": "single",
        "errors": ["pr_review_only_changed_paths_not_certified"],
    },
    "contract_not_go": {"scenario": "single", "errors": ["pr_review_only_contract_not_go"]},
    "report_result_not_pass": {"scenario": "single", "errors": ["pr_review_only_current_vc_result_not_pass"]},
}


@pytest.mark.parametrize("case", sorted(STEP4_CASES), ids=sorted(STEP4_CASES))
def test_step4_must_block_reasons(tmp_path, request, case):
    spec = STEP4_CASES[case]
    scenario: Scenario = request.getfixturevalue(spec["scenario"])
    ws = Workspace(tmp_path, scenario)
    kwargs: dict[str, Any] = {}
    report: Any = _report(scenario)
    expected = spec["errors"]

    if case == "report_not_found":
        missing = str(tmp_path / "no_such_report.json")
        kwargs["report_path"] = missing
        expected = [f"input_file_not_found:{missing}"]
    elif case == "adapt_failed_schema":
        report = {"schema": "NOT_A_TEST_VERDICT", "result": "PASS"}
    elif case == "pr_review_only_report_missing_row":
        report = _drop_pr_review_only_row(scenario)
    elif case == "duplicate_ac_command_hash":
        report = _report(scenario, extra_rows=[_row(scenario.commands[0])])
    elif case == "unknown_extra_ac_row":
        report = _report(
            scenario,
            extra_rows=[_row({"ac": "AC99", "raw_command": "echo unknown", "command_hash": "sha256:" + "9" * 64})],
        )
    elif case == "ordinary_row_missing":
        report = _report(scenario, drop=(scenario.indices("ordinary")[0],))
        kwargs["diff_summary"] = _diff_summary(scenario, changed_paths=["implemented.txt", "implemented2.txt"])
    elif case == "unknown_ac_replaces_ordinary_row":
        report = _report(
            scenario,
            drop=(0,),
            extra_rows=[_row({"ac": "AC99", "raw_command": "echo unknown", "command_hash": "sha256:" + "9" * 64})],
        )
        kwargs["diff_summary"] = _diff_summary(scenario, changed_paths=["implemented.txt", "implemented2.txt"])
    elif case == "head_drift_diff_head":
        kwargs["diff_summary"] = _diff_summary(scenario, head_sha="d" * 40)
    elif case == "head_drift_reviewed_head":
        report["reviewed_head_sha"] = "e" * 40
    elif case == "body_digest_drift":
        report = _report(scenario, body_sha256="sha256:" + "f" * 64)
    elif case == "issue_number_drift":
        report = _report(scenario, issue_number=9999)
    elif case == "pr_number_drift_diff_summary":
        kwargs["diff_summary"] = _diff_summary(scenario, pr_number=9999)
    elif case == "pr_number_drift_report":
        report = _report(scenario, pr_number=9999)
    elif case == "expected_issue_number_missing":
        kwargs["expected_issue"] = None
    elif case == "expected_pr_number_missing":
        kwargs["expected_pr"] = None
    elif case == "changed_path_outside_allowed_paths":
        kwargs["diff_summary"] = _diff_summary(scenario, changed_paths=["src/outside.py"])
    elif case == "contract_not_go":
        kwargs["snapshot"] = {**scenario.snapshot, "status": "no_go"}
    elif case == "report_result_not_pass":
        report = _report(scenario, result="PARTIAL")

    rc, payload = ws.adjudicate(report, **kwargs)

    _assert_fail_closed_no_dispatch(ws, rc, payload, expected)


def test_step4_must_block_reasons_malformed_report_json(tmp_path, single):
    ws = Workspace(tmp_path, single)
    broken = ws.write("broken_report.json", "{this is not json")

    rc, payload = ws.adjudicate(report_path=broken)

    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert len(payload["adjudication"]["errors"]) == 1
    assert payload["adjudication"]["errors"][0].startswith(f"input_json_error:{broken}:")
    assert "dispatch" not in ws.state()


# --- AC3 (adjudicate_vc_result() function layer) -----------------------------


def _envelope_current(scenario: Scenario, *, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A baseline_vc_preflight/v1 current payload carrying the REAL producer envelopes."""
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": ISSUE_NUMBER,
        "generated_at": GENERATED_AT,
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": scenario.body_sha256},
        "head_sha": scenario.head,
        "reviewed_head_sha": scenario.head,
        "results": copy.deepcopy(items if items is not None else scenario.producer["results"]),
    }


def _adjudicate_function(scenario: Scenario, current: Any, test_verdict: Any, **kwargs: Any) -> dict[str, Any]:
    return mod.adjudicate_vc_result(
        contract_snapshot=scenario.snapshot,
        current_vc_result=current,
        diff_summary=_diff_summary(scenario),
        allowed_paths=list(ALLOWED_PATHS),
        test_verdict=test_verdict,
        expected_issue_number=kwargs.pop("expected_issue_number", ISSUE_NUMBER),
        expected_pr_number=kwargs.pop("expected_pr_number", PR_NUMBER),
        **kwargs,
    )


def _adapted_current(scenario: Scenario, report: dict[str, Any]) -> dict[str, Any]:
    converted, errors = mod.adapt_test_verdict_to_current_vc_result(report)
    assert converted is not None and errors == []
    return converted


def test_adjudicator_layer_must_block_reasons_skip_envelope_echo_is_not_execution(single):
    report = _report(single)
    assert not set(TRUST_MARKER_KEYS) & set(report)

    result = _adjudicate_function(single, _envelope_current(single), report)

    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["pr_review_only_independent_requires_executed_item:AC1"]
    assert result["per_ac"] == []


def test_adjudicator_layer_must_block_reasons_missing_test_verdict(single):
    # (h) test_verdict is None: an envelope current is `test_verdict_missing`,
    # an executed current is (as today) the per-item authorization mismatch.
    envelope = _adjudicate_function(single, _envelope_current(single), None)
    assert envelope["overall_status"] != "pass" and envelope["blocking"] is True
    assert envelope["errors"] == ["test_verdict_missing"]

    executed = _adjudicate_function(single, _adapted_current(single, _report(single)), None)
    assert executed["overall_status"] != "pass" and executed["blocking"] is True
    assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"]


def test_adjudicator_layer_must_block_reasons_blanket_pass_without_current_evidence(single):
    report = _report(single)

    # (i) No current evidence at all (empty results), an envelope echo with a PASS
    # claim, and a report claiming PASS with no per-command rows are all refused.
    empty_results = _adjudicate_function(single, _envelope_current(single, items=[]), report)
    assert empty_results["overall_status"] != "pass" and empty_results["blocking"] is True
    assert empty_results["errors"] == ["pr_review_only_coverage_mismatch"]
    assert empty_results["per_ac"] == []

    rowless = _report(single, drop=(0,))
    assert rowless["result"] == "PASS" and rowless["runtime_ac_results"] == []
    converted = _adapted_current(single, rowless)
    assert converted["results"] == []
    blanket = _adjudicate_function(single, converted, rowless)
    assert blanket["overall_status"] != "pass" and blanket["blocking"] is True
    assert blanket["errors"] == ["pr_review_only_coverage_mismatch"]

    echo = _adjudicate_function(single, _envelope_current(single), report)
    assert echo["overall_status"] != "pass" and echo["errors"] == [
        "pr_review_only_independent_requires_executed_item:AC1"
    ]

    # And an executed independent PASS is the only thing that resolves the AC.
    ok = _adjudicate_function(single, _adapted_current(single, report), report)
    assert ok["overall_status"] == "pass" and ok["errors"] == []
    assert [entry["reason_code"] for entry in ok["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]


# --- AC4 ---------------------------------------------------------------------


def _dispatched(tmp_path: Path, scenario: Scenario) -> Workspace:
    """A workspace where the independent PASS was persisted and dispatch seq=1 was recorded."""
    ws = Workspace(tmp_path, scenario)
    rc, payload = ws.adjudicate(_report(scenario))
    assert rc == 0 and payload["seq"] == 1, payload
    return ws


def test_step5_must_block_reasons_reviewer_request_changes(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(
        dispatch_seq=1, verdict=_reviewer_verdict(dispatched.scenario.head, "REQUEST_CHANGES", ["blocker"])
    )

    assert rc == 1
    assert term["route"] != "approved"
    assert term["route"] == "continue_loop"


@pytest.mark.parametrize("stale_seq", [0, 2], ids=["older_seq", "newer_seq"])
def test_step5_must_block_reasons_stale_dispatch_seq(tmp_path, single, stale_seq):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(dispatch_seq=stale_seq)

    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "dispatch_seq_mismatch"
    assert term["rerun_required"]["pr_review"] is True


def test_step5_must_block_reasons_head_drift(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    drifted = "9" * 40

    rc, term = dispatched.terminal_gate(dispatch_seq=1, head=drifted)
    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"
    assert term["rerun_required"] == {"verification": True, "pr_review": True}

    # Split head: reviewer / live mergeability on a new head while the VC binding is the old one.
    rc, term = dispatched.terminal_gate(dispatch_seq=1, live_head=drifted)
    assert rc == 1 and term["route"] != "approved"


def test_step5_must_block_reasons_issue_body_drift(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(dispatch_seq=1, body="sha256:" + "8" * 64)

    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"


def test_step5_must_block_reasons_command_hash_drift(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    rc, term = dispatched.terminal_gate(dispatch_seq=1, hashes=["sha256:" + "7" * 64])

    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"


def test_step5_must_block_reasons_stale_pass_after_failed_reverification(tmp_path, single):
    dispatched = _dispatched(tmp_path, single)
    scenario = dispatched.scenario
    failing = _report(scenario, row_overrides={0: {"exit_code": 1, "status": "fail"}}, result="FAIL")

    rc, payload = dispatched.adjudicate(failing, tag="reverify")

    assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == ["pr_review_only_current_execution_not_pass:AC1"]
    assert dispatched.binding_key() not in dispatched.state().get("vc_adjudication", {})
    # The earlier reviewer APPROVE (seq 1) must not reuse the invalidated PASS.
    rc, term = dispatched.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "vc_gate_blocking"
    assert term["rerun_required"]["verification"] is True


# --- AC5 ---------------------------------------------------------------------

NON_PASS_ROWS = {
    "fail_exit_1": {"exit_code": 1, "status": "fail"},
    "skip_exit_77": {"exit_code": 77, "status": "skip"},
    "pass_status_non_zero_exit": {"exit_code": 2, "status": "pass"},
    "zero_exit_fail_status": {"exit_code": 0, "status": "fail"},
    "fallback_detected": {"fallback_detected": True},
    "human_review_required": {"human_review_required": True},
    "stop_condition_triggered": {"stop_condition_triggered": True},
}


@pytest.mark.parametrize("case", sorted(NON_PASS_ROWS), ids=sorted(NON_PASS_ROWS))
def test_execution_failure_is_fail_closed(tmp_path, single, case):
    ws = Workspace(tmp_path, single)
    report = _report(single, row_overrides={0: NON_PASS_ROWS[case]}, result="FAIL")
    report_path = ws.write("report_failure.json", report)
    before = Path(report_path).read_bytes()

    rc, payload = ws.adjudicate(report_path=report_path)

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])
    # The input report is never rewritten and nothing is covered by skip metadata.
    assert Path(report_path).read_bytes() == before
    assert payload["adjudication"]["overall_status"] == "indeterminate"
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(payload)
    # Nothing can be approved from this state either.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] != "approved", term


def test_execution_failure_is_fail_closed_keeps_ac_label_and_never_rewrites_to_pass(tmp_path, mixed):
    ws = Workspace(tmp_path, mixed)
    comma_index = [row["ac"] for row in mixed.commands].index("AC5,AC6")
    unknown_index = [row["ac"] for row in mixed.commands].index("AC_UNKNOWN")
    for tag, (index, label) in {"comma": (comma_index, "AC5,AC6"), "unknown": (unknown_index, "AC_UNKNOWN")}.items():
        report = _report(mixed, row_overrides={index: {"exit_code": 1, "status": "fail"}}, result="FAIL")
        original = copy.deepcopy(report)
        rc, payload = ws.adjudicate(
            report,
            diff_summary=_diff_summary(mixed, changed_paths=["implemented.txt", "implemented2.txt"]),
            tag=tag,
        )
        _assert_fail_closed_no_dispatch(ws, rc, payload, [f"pr_review_only_current_execution_not_pass:{label}"])
        assert json.loads((ws.dir / f"report_{tag}.json").read_text(encoding="utf-8")) == original

    # The adapter transcribes the failure verbatim; the function layer never turns it into PASS.
    report = _report(mixed, row_overrides={comma_index: {"exit_code": 1, "status": "fail"}}, result="FAIL")
    converted = _adapted_current(mixed, report)
    adapted_item = converted["results"][comma_index]
    assert (adapted_item["exit_code"], adapted_item["status"]) == (1, "fail")
    result = _adjudicate_function(mixed, converted, report)
    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["pr_review_only_current_execution_not_pass:AC5,AC6"]
    assert result["per_ac"] == []


# --- AC6 ---------------------------------------------------------------------


def _legacy_complete_test_verdict(scenario: Scenario) -> dict[str, Any]:
    """A GitHub-artifact-shaped verdict whose markers are all present and consistent."""
    command_hashes = sorted(row["command_hash"] for row in scenario.producer["results"])
    artifact_payload = {
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
        "head_sha": scenario.head,
        "reviewed_head_sha": scenario.head,
        "diff_head_sha": scenario.head,
        "contract_body_sha256": scenario.body_sha256,
        "command_hashes": command_hashes,
    }
    report = _report(scenario)
    report.update(
        {
            "workflow_run_id": 2912001,
            "workflow_run_attempt": 1,
            "check_run_id": 29120010,
            "artifact": {
                "name": "test-verdict-machine",
                "artifact_digest": "sha256:" + "a" * 64,
                "url": "https://github.com/squne121/agent-platform/actions/runs/2912001/artifacts/1",
            },
            "artifact_payload": artifact_payload,
            "artifact_payload_sha256": mod._sha256(mod._canonical_json(artifact_payload)),
        }
    )
    return report


def test_independent_and_legacy_paths_are_separated_descriptive_fields_only_is_independent(single):
    # (a) Only producer_kind / repository / run_id / run_url (no marker key) -> independent route.
    report = _report(single, descriptive=True)
    assert {"producer_kind", "repository", "run_id", "run_url"} <= set(report)
    assert not set(TRUST_MARKER_KEYS) & set(report)

    result = _adjudicate_function(single, _adapted_current(single, report), report)

    assert result["overall_status"] == "pass" and result["errors"] == []
    assert [entry["reason_code"] for entry in result["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]
    # The same report on the independent route is an envelope echo refusal, which proves
    # the route (not the evidence shape) decided: legacy would have asked for test_verdict_*.
    echo = _adjudicate_function(single, _envelope_current(single), report)
    assert echo["errors"] == ["pr_review_only_independent_requires_executed_item:AC1"]


def test_independent_and_legacy_paths_are_separated_partial_markers_stay_legacy(tmp_path, single):
    # (b) A partial marker set is legacy: the existing test_verdict_* reason, not independent evidence.
    partial = _report(single, workflow_run_id=2912001)
    result = _adjudicate_function(single, _envelope_current(single), partial)
    assert result["overall_status"] == "indeterminate" and result["blocking"] is True
    assert result["errors"] == ["test_verdict_workflow_run_attempt_invalid"]

    partial_artifact = _report(
        single, check_run_id=1, workflow_run_id=2, workflow_run_attempt=1, artifact={"name": "x"}
    )
    result = _adjudicate_function(single, _envelope_current(single), partial_artifact)
    assert result["errors"] == ["test_verdict_artifact_digest_invalid"]

    # Through the real CLI (executed current item): never `pr_review_only_independent_*`.
    ws = Workspace(tmp_path, single)
    rc, payload = ws.adjudicate(partial)
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])


@pytest.mark.parametrize("marker", TRUST_MARKER_KEYS)
@pytest.mark.parametrize(
    "placeholder", [None, "", "TODO-placeholder"], ids=["null", "empty_string", "placeholder_text"]
)
def test_independent_and_legacy_paths_are_separated_marker_key_with_empty_value_is_legacy(
    tmp_path, single, marker, placeholder
):
    # (c) The KEY alone selects the legacy route even when the value is null / "" / a placeholder.
    report = _report(single, **{marker: placeholder})
    assert marker in report

    function_result = _adjudicate_function(single, _envelope_current(single), report)
    assert function_result["overall_status"] == "indeterminate" and function_result["blocking"] is True
    assert len(function_result["errors"]) == 1
    assert function_result["errors"][0].startswith("test_verdict_"), function_result["errors"]
    assert "independent" not in function_result["errors"][0]

    ws = Workspace(tmp_path, single)
    rc, payload = ws.adjudicate(report)
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])


def test_independent_and_legacy_paths_are_separated_complete_markers_legacy_accepted(single):
    # (d) A complete, consistent marker set is still accepted by the legacy validation.
    verdict = _legacy_complete_test_verdict(single)
    assert set(TRUST_MARKER_KEYS) - {"producer_receipt", "receipt_sha256"} <= set(verdict)

    result = _adjudicate_function(single, _envelope_current(single), verdict)

    assert result["overall_status"] == "pass" and result["errors"] == [], result
    assert [entry["reason_code"] for entry in result["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]
    # An executed current item is not a legacy envelope even with complete markers.
    executed = _adjudicate_function(single, _adapted_current(single, verdict), verdict)
    assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"]


def test_independent_and_legacy_paths_are_separated_require_producer_receipt_forces_legacy(tmp_path, single):
    # (e) A marker-free independent report cannot use the independent route under
    # --require-producer-receipt; the legacy validation (incl. receipt) runs and fails closed.
    report = _report(single)

    without_flag = _adjudicate_function(single, _envelope_current(single), report)
    assert without_flag["errors"] == ["pr_review_only_independent_requires_executed_item:AC1"]

    with_flag = _adjudicate_function(single, _envelope_current(single), report, require_producer_receipt=True)
    assert with_flag["overall_status"] == "indeterminate" and with_flag["blocking"] is True
    assert with_flag["errors"] == ["test_verdict_workflow_run_id_invalid"]

    executed = _adjudicate_function(single, _adapted_current(single, report), report, require_producer_receipt=True)
    assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"]

    # Through the CLI the independent report that reaches the reviewer without the flag is refused with it.
    ws = Workspace(tmp_path, single)
    rc, payload = ws.adjudicate(report, extra_args=("--require-producer-receipt",))
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])

    # A complete marker set without a receipt still fails on the receipt under the flag.
    legacy = _adjudicate_function(
        single, _envelope_current(single), _legacy_complete_test_verdict(single), require_producer_receipt=True
    )
    assert legacy["errors"] == ["test_verdict_producer_receipt_missing"]


def test_independent_and_legacy_paths_are_separated_legacy_pins_unchanged(legacy_mixed):
    scenario = legacy_mixed
    ordinary_command, review_command = scenario.commands
    ordinary_current = {
        "ac": ordinary_command["ac"],
        "command_hash": ordinary_command["command_hash"],
        "raw_command": ordinary_command["raw_command"],
        "exit_code": 0,
        "classification": "expected_pass",
        "decision": "go",
        "scope_class": None,
        "runner": "exec",
        "failure_keys": [],
    }
    envelope = copy.deepcopy(scenario.producer["results"][1])
    assert envelope["scope_class"] == "pr_review_only" and envelope["runner"] == "skipped"
    current = _envelope_current(scenario, items=[ordinary_current, envelope])
    verdict = _legacy_complete_test_verdict(scenario)

    mixed_result = _adjudicate_function(scenario, current, verdict)

    # Legacy: in a mixed scope the pr_review_only resolved entry is EXCLUDED from per_ac.
    assert mixed_result["overall_status"] == "pass", mixed_result
    assert len(mixed_result["per_ac"]) == 1
    assert mixed_result["per_ac"][0]["ac"] == "AC1"
    assert mixed_result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"

    # Legacy: no test_verdict and an executed pr_review_only current is the authorization mismatch.
    executed_review = {
        "ac": review_command["ac"],
        "command_hash": review_command["command_hash"],
        "raw_command": review_command["raw_command"],
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "failure_keys": [],
    }
    executed_current = _envelope_current(scenario, items=[ordinary_current, executed_review])
    rejected = _adjudicate_function(scenario, executed_current, None)
    assert rejected["overall_status"] != "pass" and rejected["blocking"] is True
    assert rejected["errors"] == ["pr_review_only_current_authorization_mismatch:AC2"]


def test_independent_and_legacy_paths_are_separated_route_detection_fails_closed(tmp_path, single):
    # Refinement warning A: route detection must not treat these as independent.
    marker_free = _report(single)
    cases = {
        "wrapper_marker_free": {"TEST_VERDICT": copy.deepcopy(marker_free)},
        "wrapper_with_markers": {"TEST_VERDICT": {**copy.deepcopy(marker_free), "workflow_run_id": None}},
        "schema_mismatch_dict": {**copy.deepcopy(marker_free), "schema": "TEST_VERDICT_MACHINE/v1"},
        "empty_dict": {},
    }
    for name, verdict in cases.items():
        # Envelope current: legacy validation answers with the existing schema reason.
        result = _adjudicate_function(single, _envelope_current(single), verdict)
        assert result["overall_status"] == "indeterminate" and result["blocking"] is True, name
        assert result["errors"] == ["test_verdict_schema_mismatch"], (name, result["errors"])
        # Executed-looking current: legacy per-item authorization mismatch, never independent PASS.
        executed = _adjudicate_function(single, _adapted_current(single, marker_free), verdict)
        assert executed["overall_status"] != "pass" and executed["blocking"] is True, name
        assert executed["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"], (name, executed["errors"])

    # CLI layer: a wrapper that carries markers inside is unwrapped by the CLI and is legacy.
    ws = Workspace(tmp_path / "wrapper", single)
    ws.dir.mkdir()
    rc, payload = ws.adjudicate(cases["wrapper_with_markers"])
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_authorization_mismatch:AC1"])

    # CLI layer: a schema-mismatched dict and `{}` never reach the adjudicator (adapt fails).
    for name in ("schema_mismatch_dict", "empty_dict"):
        ws = Workspace(tmp_path / name, single)
        ws.dir.mkdir()
        rc, payload = ws.adjudicate(cases[name])
        assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
        assert payload["adjudication"]["errors"][0] == "adapt_failed", name
        assert payload["adjudication"]["errors"][1].startswith("unsupported_source_schema:"), name
        assert "dispatch" not in ws.state()


# --- AC7 ---------------------------------------------------------------------


def _squash(text: str) -> str:
    """Remove every whitespace character (incl. newlines and U+3000) before comparing."""
    return re.sub(r"\s+", "", text)


REQUIRED_PHRASES: dict[str, tuple[str, ...]] = {
    "step-2": (
        "独立経路では GitHub 固有 trust marker のキーを省略させる",
        "legacy publish 経路と `--require-producer-receipt` 指定時のみ GitHub 固有 trust marker は必須である",
    ),
    "step-4": (
        "`step4-adjudicate` の exit 0 は reviewer dispatch の許可であり、terminal approval ではない",
        "terminal approval（`termination_reason: approved` / `merge_ready: true`）の確定は "
        "`step5-terminal-gate` の exit 0 のみを根拠とする",
        "`--expected-issue-number` / `--expected-pr-number` は runtime_only または pr_review_only VC を含む場合は必須",
    ),
    "step-5": (
        "`step5-terminal-gate` に渡し、出力の `route` で分岐する",
        "terminal approval は `step5-terminal-gate` 経由に固定する",
    ),
}

FORBIDDEN_PHRASES: dict[str, tuple[str, ...]] = {
    "step-2": (),
    "step-4": ("`route_loop_verdict_v2()` の `live_mergeability` 引数として渡す",),
    "step-5": ("`route_loop_verdict_v2()` に渡し、返る `route` で分岐する",),
}

# Verbatim baseline sentences (main fc8bfe54) used to self-verify the checker.
BASELINE_STEP4_SENTENCE = (
    "mergeability（`mergeable` / `merge_state_status`）は control-plane が `gh pr view --json "
    "headRefOid,mergeable,mergeStateStatus` で都度直接取得し、`route_loop_verdict_v2()` の "
    "`live_mergeability` 引数として渡す（`step-5-mergeability-handling.md` 参照）。"
)
BASELINE_STEP5_SENTENCE = (
    "reviewer_verdict（`verdict`/`reviewed_head_sha`/`blockers`/`warnings`）と live_mergeability\n"
    "（`gh pr view` で取得した `mergeable`/`merge_state_status`）を `route_loop_verdict_v2()` に渡し、\n"
    "返る `route` で分岐する。詳細な `route` 一覧と判定条件は `step-5-mergeability-handling.md` を参照。"
)
ALLOWED_MENTIONS = {
    "step-2": "この節は `route_loop_verdict_v2_resolve_semantic_ambiguity()` を直接は呼ばない。",
    "step-4": (
        "`route_loop_verdict_v2()` によるルーティングの詳細は `step-5-mergeability-handling.md` を canonical とする。"
    ),
    "step-5": (
        "`step5-terminal-gate` は公開 wrapper `route_loop_verdict_v2_resolve_semantic_ambiguity()` を呼ぶ。"
        "approved 以外の route は `route_loop_verdict_v2()` の評価結果として再評価する。"
        "terminal approval を `route_loop_verdict_v2()` の直接呼び出し結果だけで確定してはならない。"
    ),
}


def _doc_problems(docs: dict[str, str]) -> list[str]:
    """Exact (whitespace-stripped) substring presence / absence. No classifier, no rg."""
    problems: list[str] = []
    for name, text in docs.items():
        squashed = _squash(text)
        for phrase in REQUIRED_PHRASES[name]:
            if _squash(phrase) not in squashed:
                problems.append(f"{name}: missing required: {phrase}")
        for phrase in FORBIDDEN_PHRASES[name]:
            if _squash(phrase) in squashed:
                problems.append(f"{name}: forbidden present: {phrase}")
    return problems


def _fixture_docs(*, with_required: bool, with_forbidden: bool) -> dict[str, str]:
    docs: dict[str, str] = {}
    for name in ("step-2", "step-4", "step-5"):
        parts = [ALLOWED_MENTIONS[name]]
        if with_required:
            # Required sentences laid out with arbitrary line breaks / spaces: only content counts.
            parts.extend(phrase.replace(" ", "\n  ") for phrase in REQUIRED_PHRASES[name])
        if with_forbidden:
            parts.extend(FORBIDDEN_PHRASES[name])
        docs[name] = "\n\n".join(parts)
    if with_forbidden:
        docs["step-4"] += "\n\n" + BASELINE_STEP4_SENTENCE
        docs["step-5"] += "\n\n" + BASELINE_STEP5_SENTENCE
    return docs


def test_step_docs_state_canonical_entrance_checker_self_verifies():
    # Post-implementation shape: required present, forbidden absent, allowed mentions tolerated.
    assert _doc_problems(_fixture_docs(with_required=True, with_forbidden=False)) == []

    # Baseline shape (verbatim old sentences, no required sentence): both kinds of problem appear.
    baseline_like = {
        "step-2": "",
        "step-4": BASELINE_STEP4_SENTENCE,
        "step-5": BASELINE_STEP5_SENTENCE
        + "\n\n### terminal approval は `step5-terminal-gate` 経由に固定する（Issue #2837）",
    }
    problems = _doc_problems(baseline_like)
    assert any(problem.startswith("step-2: missing required") for problem in problems)
    assert any(problem.startswith("step-4: missing required") for problem in problems)
    assert any(problem.startswith("step-4: forbidden present") for problem in problems)
    assert any(problem.startswith("step-5: missing required") for problem in problems)
    assert any(problem.startswith("step-5: forbidden present") for problem in problems)

    # Each required / forbidden phrase is individually load-bearing.
    for name, phrases in REQUIRED_PHRASES.items():
        for phrase in phrases:
            docs = _fixture_docs(with_required=True, with_forbidden=False)
            docs[name] = docs[name].replace(phrase.replace(" ", "\n  "), "")
            assert any("missing required" in problem for problem in _doc_problems(docs)), phrase
    for name, phrases in FORBIDDEN_PHRASES.items():
        for phrase in phrases:
            docs = _fixture_docs(with_required=True, with_forbidden=False)
            docs[name] += "\n" + phrase.replace(" ", "\n")
            assert any("forbidden present" in problem for problem in _doc_problems(docs)), phrase

    # A table split by `|` would not satisfy the single-sentence requirement (warning C).
    split = (
        "| `--expected-issue-number` / `--expected-pr-number` | "
        "runtime_only または pr_review_only VC を含む場合は必須 |"
    )
    docs = _fixture_docs(with_required=True, with_forbidden=False)
    docs["step-4"] = docs["step-4"].replace(REQUIRED_PHRASES["step-4"][2].replace(" ", "\n  "), "") + "\n" + split
    assert any("missing required" in problem for problem in _doc_problems(docs))


def test_step_docs_state_canonical_entrance(tmp_path):
    docs = {
        "step-2": STEP2_DOC.read_text(encoding="utf-8"),
        "step-4": STEP4_DOC.read_text(encoding="utf-8"),
        "step-5": STEP5_DOC.read_text(encoding="utf-8"),
    }

    assert _doc_problems(docs) == []
    # The retained, allowed texts are still present (they are outside the checked phrases).
    assert "route_loop_verdict_v2_resolve_semantic_ambiguity()" in docs["step-5"]
    assert "step-5-mergeability-handling.md" in docs["step-4"]


# --- #2924 REQUEST_CHANGES fix_delta (A: ordinary status / B: raw report / C: Step 2 triplet) ---


@pytest.mark.parametrize("status", ["skip", "fail"])
def test_mixed_ordinary_non_pass_status_with_zero_exit_is_not_promoted_to_pass(tmp_path, mixed, status):
    # Finding A (P1): an ordinary row whose own status is not "pass" must not be promoted
    # to PASS merely because exit_code == 0 while the report-level result is PASS.
    changed = ["implemented.txt", "implemented2.txt"]
    for tag, index in zip(("first", "last"), (mixed.indices("ordinary")[0], mixed.indices("ordinary")[-1])):
        ws = Workspace(tmp_path / tag, mixed)
        ws.dir.mkdir()
        report = _report(mixed, row_overrides={index: {"status": status, "exit_code": 0}})
        assert report["result"] == "PASS"

        rc, payload = ws.adjudicate(report, diff_summary=_diff_summary(mixed, changed_paths=changed))

        assert rc == 1, (tag, payload)
        assert payload["invoke_pr_reviewer"] is False, (tag, payload)
        assert payload["seq"] is None
        assert payload["adjudication"]["blocking"] is True
        assert "dispatch" not in ws.state()
        assert ws.state().get("vc_adjudication", {}) == {}


@pytest.mark.parametrize("flag", ["fallback_detected", "human_review_required", "stop_condition_triggered"])
@pytest.mark.parametrize("bad", ["missing", None, 0, "", "false"], ids=["missing", "null", "zero", "empty", "str"])
def test_raw_report_execution_flags_must_be_explicit_booleans(tmp_path, single, flag, bad):
    # Finding B-1 (P2): a missing / null / non-bool flag in the RAW report is not an explicit false.
    ws = Workspace(tmp_path, single)
    report = _report(single)
    if bad == "missing":
        del report["runtime_ac_results"][0][flag]
    else:
        report["runtime_ac_results"][0][flag] = bad

    rc, payload = ws.adjudicate(report)

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    # A truthy non-bool ("false") is coerced to True by the adapter and already blocked as a
    # non-pass execution; every other non-bool is rejected on the raw report. Both fail closed.
    expected = (
        ["pr_review_only_current_execution_not_pass:AC1"]
        if bad == "false"
        else [f"pr_review_only_report_flag_not_boolean:AC1:{flag}"]
    )
    assert payload["adjudication"]["errors"] == expected
    assert "dispatch" not in ws.state()


def test_raw_report_flags_are_checked_for_ordinary_rows_and_top_level(tmp_path, mixed):
    changed = _diff_summary(mixed, changed_paths=["implemented.txt", "implemented2.txt"])
    ordinary = mixed.indices("ordinary")[0]
    ws = Workspace(tmp_path / "row", mixed)
    ws.dir.mkdir()
    report = _report(mixed)
    del report["runtime_ac_results"][ordinary]["stop_condition_triggered"]
    rc, payload = ws.adjudicate(report, diff_summary=changed)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    assert payload["adjudication"]["errors"] == ["pr_review_only_report_flag_not_boolean:AC1:stop_condition_triggered"]

    ws = Workspace(tmp_path / "top", mixed)
    ws.dir.mkdir()
    rc, payload = ws.adjudicate(_report(mixed, human_review_required=0), diff_summary=changed)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    assert payload["adjudication"]["errors"] == ["pr_review_only_report_flag_not_boolean::human_review_required"]


@pytest.mark.parametrize("field", ["reviewed_head_sha", "diff_head_sha"])
@pytest.mark.parametrize("bad", ["missing", None, "", "d" * 40], ids=["missing", "null", "empty", "drift"])
def test_raw_report_head_fields_are_required_and_must_agree(tmp_path, single, field, bad):
    # Finding B-2 (P2): step-2 requires head_sha / reviewed_head_sha / diff_head_sha bound to one head.
    # reviewed_head_sha must not be back-filled from head_sha, and a drifting diff_head_sha is not ignored.
    ws = Workspace(tmp_path, single)
    report = _report(single)
    if bad == "missing":
        del report[field]
    else:
        report[field] = bad

    rc, payload = ws.adjudicate(report)

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["adjudication"]["errors"] == ["pr_review_only_head_binding_mismatch"]
    assert "dispatch" not in ws.state()


def _step2_message_template() -> str:
    text = STEP2_DOC.read_text(encoding="utf-8")
    block = re.search(r"```yaml\n(spawn_agent:.*?)```", text, flags=re.DOTALL)
    assert block is not None, "step-2 spawn_agent template not found"
    return block.group(1)


def test_step2_delegation_template_passes_ac_command_command_hash_triplets_verbatim():
    # Finding C (P1): test-runner must echo command_hash, so root passes (ac, raw_command, command_hash)
    # triplets from extract-vc-metadata verbatim; test-runner never guesses / recomputes / inherits it.
    template = _step2_message_template()
    squashed_template = _squash(template)
    assert _squash("Per-command inputs") in squashed_template
    assert _squash("echo these values exactly") in squashed_template
    for key in ("- ac:", "command:", "command_hash:"):
        assert _squash(key) in squashed_template, key
    # The old pair-only (label => command) shape must be gone from the template.
    assert "=>" not in template

    doc = _squash(STEP2_DOC.read_text(encoding="utf-8"))
    for phrase in (
        "`(ac, raw_command, command_hash)` の triplet",
        "`extract-vc-metadata` の `commands[]` から",
        "test-runner は `command_hash` を推測・再計算してはならず、親 context の暗黙継承に依存してはならない",
    ):
        assert _squash(phrase) in doc, phrase
    assert _squash("`(ac label, literal command)` の組") not in doc


# =============================================================================
# Issue #3020: executed-fixture integration regression.
#
# Everything above builds its PASS report by hand (``_row()`` defaults to
# ``exit_code=0`` / ``status="pass"``), which proves the consumer's input-shape
# validation but not that an EXECUTED VC result reaches it. The helpers below run
# a small local pytest fixture in a real subprocess and build the report rows
# from the OBSERVED run only. The producer (``baseline_vc_preflight.py``) and the
# adjudicator (``adjudicate_vc_result.py step4-adjudicate``) stay real; nothing is
# mocked.
#
# Binding model (one literal command end to end):
#   declared VC (body ``raw_command``) == executed command (shlex.split -> argv)
#   == report row ``command`` / ``command_hash`` (looked up in the scenario's
#   ``commands`` by that exact literal) and the observed run happened in the
#   scenario's own git repo at ``scenario.head`` with a clean tracked tree.
# Execution proof: exit 0 alone is NOT an executed PASS (``--collect-only`` exits
# 0 without running anything). The fixture test writes a call-phase witness
# after its assertion succeeded; only exit 0 + that witness is "pass".
# https://docs.pytest.org/en/9.0.x/reference/reference.html
# =============================================================================

FIXTURE_TEST_PATH = "fixture_tests/test_fixture_3020.py"
FIXTURE_TEST_NAME = "test_executed_fixture_deferred_case"
FIXTURE_K_EXPR = "executed_fixture and deferred"
FIXTURE_K_EXPR_OTHER = "executed_fixture"
FIXTURE_K_EXPR_UNCOLLECTED = "no_such_test_name_anywhere"
WITNESS_FILE_ENV = "LP3020_WITNESS_FILE"
WITNESS_NONCE_ENV = "LP3020_WITNESS_NONCE"
REPAIR_SCRIPT_PATH = SKILL_DIR.parent / "issue-refinement-loop" / "scripts" / "repair_issue_contract.py"

# Caches the pytest child may create; keep the scenario repo clean without touching pytest's argv.
FIXTURE_GITIGNORE = ".pytest_cache/\n__pycache__/\n"


def _fixture_source(assertion: str = "True") -> str:
    """The fixture test. The witness is written AFTER the assertion, so it exists only if the test body
    really ran in the call phase and the assertion held (a failing assertion raises before the write)."""
    return (
        "import json\n"
        "import os\n"
        "\n"
        "\n"
        f"def {FIXTURE_TEST_NAME}():\n"
        f"    assert {assertion}\n"
        f"    witness = os.environ.get({WITNESS_FILE_ENV!r})\n"
        "    if witness:\n"
        "        nodeid, _, phase = os.environ['PYTEST_CURRENT_TEST'].rpartition(' (')\n"
        "        with open(witness, 'w', encoding='utf-8') as handle:\n"
        "            json.dump(\n"
        "                {\n"
        "                    'nodeid': nodeid,\n"
        "                    'phase': phase.rstrip(')'),\n"
        "                    'assertion': 'passed',\n"
        f"                    'nonce': os.environ.get({WITNESS_NONCE_ENV!r}),\n"
        "                },\n"
        "                handle,\n"
        "            )\n"
    )


def _repo_files(source: str | None = None) -> dict[str, str]:
    return {FIXTURE_TEST_PATH: source or _fixture_source(), ".gitignore": FIXTURE_GITIGNORE}


# The declared VC is an executable literal (not `uv run --locked`, which would need a lockfile the local
# fixture repo does not have, and whose equivalence to a direct interpreter run is not assumed).
_BODY_3020_TEMPLATE = """## Allowed Paths
- tracked.txt
- {fixture_path}

## Verification Commands

```bash
# AC1
{annotation}
$ python -m pytest {fixture_path} -q -k "{k_expr}"
```
"""


def _body_3020(annotation: str, k_expr: str = FIXTURE_K_EXPR) -> str:
    return _BODY_3020_TEMPLATE.format(annotation=annotation, fixture_path=FIXTURE_TEST_PATH, k_expr=k_expr)


BODY_DEFERRED_RAW = _body_3020("# baseline-expect: deferred")
BODY_PR_REVIEW_ONLY = _body_3020("# preflight-scope: pr_review_only")


class BindingMismatch(Exception):
    """The executed observation does not bind to the scenario's declared VC / head."""


@dataclass
class FixtureObservation:
    """What the local pytest subprocess actually did (no assumed outcome)."""

    exit_code: int
    output: str
    executed: bool  # True only for an observation that really ran the test body (pass / red)
    kind: str  # "pass" | "red" | "not_executed" | "uncollected" | "launch_obstacle"
    executed_command: str  # the literal declared command that was split into argv
    argv: list[str]
    cwd: str
    head: str
    launcher_substituted: str | None
    selector: str | None
    target_path: str | None
    witness: dict[str, Any] | None


def _valid_witness(witness: Any, nonce: str | None) -> bool:
    return (
        isinstance(witness, dict)
        and nonce is not None
        and witness.get("nodeid") == f"{FIXTURE_TEST_PATH}::{FIXTURE_TEST_NAME}"
        and witness.get("phase") == "call"
        and witness.get("assertion") == "passed"
        and witness.get("nonce") == nonce
    )


def _classify_pytest_observation(
    exit_code: int, output: str, witness: Any = None, nonce: str | None = None
) -> tuple[bool, str]:
    """Classify an OBSERVED pytest run -> (executed, kind).

    exit 0 is a PASS only when the test-local call-phase witness is present and matches (``--collect-only``
    exits 0 without running anything). Only an exit-1 run reporting an assertion failure is RED; exit 5
    (nothing collected) and pre-launch obstacles (uv / usage / launch errors) are never RED (AC6)."""
    if exit_code == 0:
        if _valid_witness(witness, nonce):
            return True, "pass"
        return False, "not_executed"
    if exit_code == 5:
        return False, "uncollected"
    if exit_code == 1 and re.search(r"^(FAILED|E\s+assert)", output, flags=re.MULTILINE):
        return True, "red"
    return False, "launch_obstacle"


def _assert_repo_pristine(repo: Path, expected_head: str) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    assert head == expected_head, f"repo HEAD {head} != scenario head {expected_head}"
    dirty = _git(repo, "status", "--porcelain")
    assert dirty == "", f"repo is not clean: {dirty!r}"


def _child_env(witness_file: Path, nonce: str, extra_env: dict[str, str] | None) -> dict[str, str]:
    """Child env: parent pytest options (PYTEST_ADDOPTS / PYTEST_PLUGINS / PYTEST_CURRENT_TEST ...) are
    removed by default so the parent run cannot change the child's behaviour. ``extra_env`` re-adds them
    explicitly (negative tests)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", WITNESS_FILE_ENV: str(witness_file), WITNESS_NONCE_ENV: nonce})
    env.update(extra_env or {})
    return env


def _selector_and_target(argv: list[str]) -> tuple[str | None, str | None]:
    selector = argv[argv.index("-k") + 1] if "-k" in argv and argv.index("-k") + 1 < len(argv) else None
    targets = [token for token in argv if token.endswith(".py") or "::" in token]
    return selector, (targets[0] if targets else None)


def _run_local_pytest_fixture(
    repo: Path,
    expected_head: str,
    raw_command: str,
    *,
    witness_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> FixtureObservation:
    """Execute the DECLARED literal command (``shlex.split``) in the scenario repo at ``expected_head``.

    Only the launcher word ``python`` / ``python3`` is replaced by ``sys.executable`` (recorded in the
    observation); cwd / selector / target path are recorded too."""
    _assert_repo_pristine(repo, expected_head)
    argv = shlex.split(raw_command)
    substituted = None
    if argv and argv[0] in ("python", "python3"):
        substituted = f"{argv[0]} -> {sys.executable}"
        argv[0] = sys.executable
    witness_dir.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    witness_file = witness_dir / f"witness_{nonce}.json"  # outside the repo
    completed = subprocess.run(
        argv,
        cwd=repo,
        env=_child_env(witness_file, nonce, extra_env),
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    witness = json.loads(witness_file.read_text(encoding="utf-8")) if witness_file.exists() else None
    executed, kind = _classify_pytest_observation(completed.returncode, output, witness, nonce)
    selector, target = _selector_and_target(argv)
    return FixtureObservation(
        exit_code=completed.returncode,
        output=output,
        executed=executed,
        kind=kind,
        executed_command=raw_command,
        argv=argv,
        cwd=str(repo),
        head=_git(repo, "rev-parse", "HEAD"),
        launcher_substituted=substituted,
        selector=selector,
        target_path=target,
        witness=witness,
    )


def _run_scenario_vc(
    scenario: Scenario,
    tmp_path: Path,
    *,
    raw_command: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> FixtureObservation:
    assert scenario.repo is not None
    return _run_local_pytest_fixture(
        scenario.repo,
        scenario.head,
        raw_command if raw_command is not None else scenario.commands[0]["raw_command"],
        witness_dir=tmp_path / "witness",
        extra_env=extra_env,
    )


def _report_from_observation(scenario: Scenario, observation: FixtureObservation) -> dict[str, Any]:
    """Build the independent test-runner report from the observation ONLY.

    The row command / command_hash / ac come from the ``scenario.commands`` entry whose ``raw_command`` is
    exactly the executed literal; the head is the observed head. Nothing is filled in from the scenario
    unconditionally, and a command or head that does not bind raises ``BindingMismatch``.
    ``result`` is derived from the observation (PASS only for an executed pass), never forced.
    """
    matches = [i for i, c in enumerate(scenario.commands) if c["raw_command"] == observation.executed_command]
    if len(matches) != 1:
        raise BindingMismatch(
            f"command binding mismatch: executed {observation.executed_command!r} is not a declared "
            f"raw_command of scenario {scenario.name!r}"
        )
    if observation.head != scenario.head:
        raise BindingMismatch(f"head binding mismatch: observed {observation.head} != scenario {scenario.head}")
    index = matches[0]
    command = scenario.commands[index]
    passed = observation.kind == "pass"
    tail = observation.output.strip().splitlines()[-1] if observation.output.strip() else ""
    row = {
        "command_hash": command["command_hash"],
        "exit_code": observation.exit_code,
        "status": "pass" if passed else "fail",
        "notes": f"{observation.kind}; cwd={observation.cwd}; head={observation.head}; {tail}",
    }
    report = _report(
        scenario,
        head=observation.head,
        drop=tuple(i for i in range(len(scenario.commands)) if i != index),
        row_overrides={index: row},
        result="PASS" if passed else "FAIL",
    )
    # ``_row`` takes the command positionally, so the literal that was executed is set explicitly here.
    report["runtime_ac_results"][0]["command"] = observation.executed_command
    return report


def _apply_real_repair(tmp_path: Path, body: str) -> tuple[dict[str, Any], str]:
    body_file = tmp_path / "raw_body.md"
    out_file = tmp_path / "repaired_body.md"
    body_file.write_text(body, encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(REPAIR_SCRIPT_PATH),
            "--body-file",
            str(body_file),
            "--apply",
            "--out-file",
            str(out_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout), out_file.read_text(encoding="utf-8")


def _executed_ws(tmp_path: Path, scenario: Scenario) -> Workspace:
    return Workspace(tmp_path, scenario)


def _adjudicate_executed(ws: Workspace, report: dict[str, Any], **kwargs: Any) -> tuple[int, dict[str, Any]]:
    return ws.adjudicate(
        report,
        diff_summary=_diff_summary(ws.scenario, changed_paths=[FIXTURE_TEST_PATH]),
        allowed=[*ALLOWED_PATHS, FIXTURE_TEST_PATH],
        **kwargs,
    )


@pytest.fixture(scope="module")
def deferred_raw(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    scenario = _build_scenario(tmp_path_factory, "deferred_raw", BODY_DEFERRED_RAW, repo_files=_repo_files())
    item = scenario.producer["results"][0]
    # The REAL producer's classification of the raw annotation -- not the canonical pr_review_only envelope.
    assert item["category"] == "baseline_expect_deferred", item
    assert item["classification"] == "skipped", item
    return scenario


def _canonical_scenario(
    factory: pytest.TempPathFactory,
    name: str,
    *,
    k_expr: str = FIXTURE_K_EXPR,
    source: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> Scenario:
    scenario = _build_scenario(
        factory,
        name,
        _body_3020("# preflight-scope: pr_review_only", k_expr),
        repo_files={**_repo_files(source), **(extra_files or {})},
    )
    item = scenario.producer["results"][0]
    assert item["category"] == "preflight_scope_pr_review_only", item
    assert item["scope_class"] == "pr_review_only", item
    assert scenario.commands[0]["raw_command"].startswith("python -m pytest "), scenario.commands
    return scenario


@pytest.fixture(scope="module")
def canonical_pr_review_only(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    return _canonical_scenario(tmp_path_factory, "pr_review_only_3020")


@pytest.fixture(scope="module")
def canonical_other_selector(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    """Same fixture and annotation, but a DIFFERENT `-k` selector -> a different command hash."""
    # An extra committed file keeps its HEAD distinct from the first scenario's (same-second commits would
    # otherwise hash identically).
    return _canonical_scenario(
        tmp_path_factory,
        "pr_review_only_other_selector",
        k_expr=FIXTURE_K_EXPR_OTHER,
        extra_files={"other_selector_marker.txt": "other selector scenario\n"},
    )


@pytest.fixture(scope="module")
def canonical_red(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    return _canonical_scenario(tmp_path_factory, "pr_review_only_red", source=_fixture_source("1 == 2"))


@pytest.fixture(scope="module")
def canonical_uncollected(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    """The DECLARED selector matches nothing, so the honest run is pytest exit 5."""
    return _canonical_scenario(tmp_path_factory, "pr_review_only_uncollected", k_expr=FIXTURE_K_EXPR_UNCOLLECTED)


def test_executed_fixture_observation_classification_is_real_and_exit5_or_launch_failure_is_not_red(tmp_path):
    """AC6: classify REAL subprocess observations; exit 5 and a pre-launch obstacle are never RED."""
    declared = f'python -m pytest {FIXTURE_TEST_PATH} -q -k "{FIXTURE_K_EXPR}"'

    def run(name: str, files: dict[str, str], command: str = declared) -> FixtureObservation:
        repo = tmp_path / name / "repo"
        head = _init_repo(repo, files)
        return _run_local_pytest_fixture(repo, head, command, witness_dir=tmp_path / name / "w")

    ok = run("ok", _repo_files())
    assert (ok.exit_code, ok.kind, ok.executed) == (0, "pass", True), ok.output
    assert ok.witness is not None and ok.witness["phase"] == "call", ok.witness

    red = run("red", _repo_files(_fixture_source("1 == 2")))
    assert (red.exit_code, red.kind, red.executed) == (1, "red", True), red.output
    assert red.witness is None  # the assertion failed before the witness write

    # `-k` is partial matching; a selector matching nothing is exit 5 (not collected), never RED.
    uncollected = run(
        "unc", _repo_files(), f'python -m pytest {FIXTURE_TEST_PATH} -q -k "{FIXTURE_K_EXPR_UNCOLLECTED}"'
    )
    assert (uncollected.exit_code, uncollected.kind, uncollected.executed) == (5, "uncollected", False)

    # A real `uv run --locked` launch obstacle (no lockfile) is a pre-launch failure, not RED.
    obstacle = run(
        "uvproj",
        {**_repo_files(), "pyproject.toml": '[project]\nname = "x"\nversion = "0"\n'},
        f'uv run --locked pytest {FIXTURE_TEST_PATH} -q -k "{FIXTURE_K_EXPR}"',
    )
    assert obstacle.exit_code != 0, obstacle.output
    assert obstacle.kind == "launch_obstacle" and obstacle.executed is False, obstacle.output
    assert "lockfile" in obstacle.output
    assert obstacle.launcher_substituted is None  # only the `python` launcher word is replaced

    # Synthetic classifier boundaries: only exit 1 with an assertion failure is RED.
    assert _classify_pytest_observation(1, "usage: pytest ...")[1] == "launch_obstacle"
    assert _classify_pytest_observation(4, "ERROR: usage")[1] == "launch_obstacle"
    assert _classify_pytest_observation(127, "uv: command not found")[1] == "launch_obstacle"
    # exit 0 without a (valid) call-phase witness is never an executed pass.
    good = {
        "nodeid": f"{FIXTURE_TEST_PATH}::{FIXTURE_TEST_NAME}",
        "phase": "call",
        "assertion": "passed",
        "nonce": "n1",
    }
    assert _classify_pytest_observation(0, "", good, "n1") == (True, "pass")
    assert _classify_pytest_observation(0, "", None, "n1") == (False, "not_executed")
    for forged in (
        {**good, "nonce": "other"},
        {**good, "phase": "setup"},
        {**good, "assertion": "failed"},
        {**good, "nodeid": "x.py::y"},
    ):
        assert _classify_pytest_observation(0, "", forged, "n1") == (False, "not_executed"), forged


def test_executed_fixture_run_is_bound_to_declared_command_repo_and_head(tmp_path, canonical_pr_review_only):
    """P2-4: the executed argv is derived from the declared literal, run in the scenario repo at its head
    (fixture committed there, clean), and the observation records what actually ran."""
    scenario = canonical_pr_review_only
    declared = scenario.commands[0]["raw_command"]
    assert scenario.repo is not None
    assert _git(scenario.repo, "rev-parse", "HEAD") == scenario.head
    assert FIXTURE_TEST_PATH in _git(scenario.repo, "ls-tree", "-r", "--name-only", scenario.head).splitlines()
    # The producer ran in this same repo.
    assert scenario.producer["results"][0]["raw_command"] == declared

    observation = _run_scenario_vc(scenario, tmp_path)

    assert observation.kind == "pass" and observation.executed, observation.output
    assert observation.executed_command == declared
    assert observation.cwd == str(scenario.repo) and observation.head == scenario.head
    assert observation.argv == [sys.executable, *shlex.split(declared)[1:]]
    assert observation.launcher_substituted == f"python -> {sys.executable}"
    assert observation.selector == FIXTURE_K_EXPR and observation.target_path == FIXTURE_TEST_PATH
    assert _git(scenario.repo, "status", "--porcelain") == ""  # the run left the repo clean

    report = _report_from_observation(scenario, observation)
    row = report["runtime_ac_results"][0]
    assert (row["command"], row["command_hash"]) == (declared, scenario.commands[0]["command_hash"])
    assert report["head_sha"] == scenario.head and report["result"] == "PASS"


def test_executed_fixture_refuses_to_run_in_a_dirty_or_moved_repo(tmp_path):
    repo = tmp_path / "repo"
    head = _init_repo(repo, _repo_files())
    declared = f'python -m pytest {FIXTURE_TEST_PATH} -q -k "{FIXTURE_K_EXPR}"'
    (repo / "tracked.txt").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="not clean"):
        _run_local_pytest_fixture(repo, head, declared, witness_dir=tmp_path / "w")
    _git(repo, "checkout", "--", "tracked.txt")
    with pytest.raises(AssertionError, match="HEAD"):
        _run_local_pytest_fixture(repo, "0" * 40, declared, witness_dir=tmp_path / "w")


def test_executed_fixture_child_env_strips_parent_pytest_options_by_default(
    tmp_path, monkeypatch, canonical_pr_review_only
):
    """P2-3: a parent PYTEST_ADDOPTS must not leak into the child (default), so the run executes for real."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    monkeypatch.setenv("PYTEST_PLUGINS", "no_such_plugin_module_3020")
    observation = _run_scenario_vc(canonical_pr_review_only, tmp_path)
    assert observation.kind == "pass" and observation.executed, observation.output
    assert observation.witness is not None


def test_executed_fixture_collect_only_exit0_without_witness_is_not_pass_and_not_dispatched(
    tmp_path, canonical_pr_review_only
):
    """P2-3 negative: `PYTEST_ADDOPTS=--collect-only` is actually handed to the child. pytest exits 0 without
    running the test; no witness exists, so the observation is not a pass, the report is not PASS, and the
    real step4-adjudicate does not dispatch the reviewer."""
    observation = _run_scenario_vc(canonical_pr_review_only, tmp_path, extra_env={"PYTEST_ADDOPTS": "--collect-only"})
    assert observation.exit_code == 0, observation.output  # the false-PASS precondition
    assert "1 test collected" in observation.output, observation.output
    assert observation.witness is None
    assert (observation.kind, observation.executed) == ("not_executed", False)

    report = _report_from_observation(canonical_pr_review_only, observation)
    assert report["result"] == "FAIL"
    assert report["runtime_ac_results"][0]["status"] == "fail"

    ws = _executed_ws(tmp_path / "ws", canonical_pr_review_only)
    ws.dir.mkdir()
    rc, payload = _adjudicate_executed(ws, report)

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(payload)


def test_executed_fixture_other_selector_observation_is_rejected_as_binding_mismatch(
    tmp_path, canonical_pr_review_only, canonical_other_selector
):
    """P2-4 negative: a run of a DIFFERENT `-k` selector (or of another repo head) cannot be used as the
    evidence of the declared VC. It is rejected as a binding mismatch, and as its own scenario it carries
    its own command hash."""
    scenario_a, scenario_b = canonical_pr_review_only, canonical_other_selector
    command_a, command_b = scenario_a.commands[0], scenario_b.commands[0]
    assert command_a["raw_command"] != command_b["raw_command"]
    assert command_a["command_hash"] != command_b["command_hash"]

    # B's selector executed in A's repo is a genuine pass, but not of A's declared command.
    wrong = _run_scenario_vc(scenario_a, tmp_path / "wrong", raw_command=command_b["raw_command"])
    assert wrong.kind == "pass" and wrong.head == scenario_a.head, wrong.output
    assert wrong.selector == FIXTURE_K_EXPR_OTHER
    with pytest.raises(BindingMismatch, match="command binding mismatch"):
        _report_from_observation(scenario_a, wrong)

    # A's command executed in B's repo: the command is declared in A, but the head is B's.
    moved = _run_scenario_vc(scenario_b, tmp_path / "moved", raw_command=command_a["raw_command"])
    assert moved.kind == "pass" and moved.head == scenario_b.head and scenario_a.head != scenario_b.head
    with pytest.raises(BindingMismatch, match="head binding mismatch"):
        _report_from_observation(scenario_a, moved)

    # The same observation is valid evidence only for the scenario whose declared VC it executed.
    right = _run_scenario_vc(scenario_b, tmp_path / "right")
    report_b = _report_from_observation(scenario_b, right)
    row = report_b["runtime_ac_results"][0]
    assert (row["command"], row["command_hash"]) == (command_b["raw_command"], command_b["command_hash"])
    assert row["command_hash"] != command_a["command_hash"]

    # And the real adjudicator accepts B's report only against B's own binding.
    ws = _executed_ws(tmp_path / "ws_b", scenario_b)
    ws.dir.mkdir()
    rc, payload = _adjudicate_executed(ws, report_b)
    assert rc == 0 and payload["invoke_pr_reviewer"] is True, payload


def test_executed_fixture_deferred_raw_annotation_rejected_as_unsupported_classification(tmp_path, deferred_raw):
    """AC5(a): raw `baseline-expect: deferred` + a valid, actually-executed PASS -> the real step4-adjudicate
    rejects the baseline classification and does not dispatch the reviewer."""
    observation = _run_scenario_vc(deferred_raw, tmp_path / "fx")
    assert observation.kind == "pass" and observation.executed, observation.output
    report = _report_from_observation(deferred_raw, observation)
    assert report["result"] == "PASS"
    ws = _executed_ws(tmp_path / "ws", deferred_raw)
    ws.dir.mkdir()

    rc, payload = _adjudicate_executed(ws, report)

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    errors = payload["adjudication"]["errors"]
    assert errors == ["unsupported_baseline_classification:AC1"], payload
    assert payload["adjudication"]["blocking"] is True
    assert "dispatch" not in ws.state()


def test_executed_fixture_deferred_binding_mismatch_is_a_distinct_cause(
    tmp_path, deferred_raw, canonical_pr_review_only
):
    """AC5: the error side is judged on `adjudication.errors`, so a head / body / command binding mismatch is
    identifiable as its own cause and is not confused with the deferred classification rejection."""
    observation = _run_scenario_vc(canonical_pr_review_only, tmp_path / "fx")
    assert observation.kind == "pass", observation.output

    # Canonical scenario + drifting bindings: the errors are binding errors, never the deferred error.
    ws = _executed_ws(tmp_path / "ws_canon", canonical_pr_review_only)
    ws.dir.mkdir()
    report = _report_from_observation(canonical_pr_review_only, observation)
    drift_head = dict(report, reviewed_head_sha="e" * 40)
    rc, payload = _adjudicate_executed(ws, drift_head)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    assert payload["adjudication"]["errors"] == ["pr_review_only_head_binding_mismatch"], payload

    ws2 = _executed_ws(tmp_path / "ws_body", canonical_pr_review_only)
    ws2.dir.mkdir()
    drift_body = _report_from_observation(canonical_pr_review_only, observation)
    drift_body["contract_body_sha256"] = "sha256:" + "f" * 64
    rc, payload = _adjudicate_executed(ws2, drift_body)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    assert payload["adjudication"]["errors"] == ["pr_review_only_source_body_sha256_mismatch"], payload

    # Deferred scenario + the same drift: the classification error is reported FIRST and alone, so the two
    # causes are distinguishable by exact `errors` content (never a generic top-level reason only).
    deferred_observation = _run_scenario_vc(deferred_raw, tmp_path / "fx_deferred")
    assert deferred_observation.kind == "pass", deferred_observation.output
    ws3 = _executed_ws(tmp_path / "ws_deferred_drift", deferred_raw)
    ws3.dir.mkdir()
    drifted = _report_from_observation(deferred_raw, deferred_observation)
    drifted["reviewed_head_sha"] = "e" * 40
    rc, payload = _adjudicate_executed(ws3, drifted)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    assert payload["adjudication"]["errors"] == ["unsupported_baseline_classification:AC1"], payload
    assert payload["reason_code"] == "adjudication_missing_or_malformed"


def test_executed_fixture_deferred_normalized_to_pr_review_only_reaches_reviewer_dispatch(tmp_path, tmp_path_factory):
    """AC5(b): the real repair normalizes `baseline-expect: deferred` to `preflight-scope: pr_review_only`
    (annotation only), the real producer then yields the canonical envelope, and an actually-executed PASS
    of the same declared literal at the same head makes the real step4-adjudicate dispatch the reviewer; the
    real terminal gate approves."""
    producer_result, repaired_body = _apply_real_repair(tmp_path, BODY_DEFERRED_RAW)
    assert producer_result["repair_action"]["disposition"] == "auto_apply_safe"
    assert repaired_body == BODY_PR_REVIEW_ONLY

    scenario = _build_scenario(tmp_path_factory, "deferred_normalized", repaired_body, repo_files=_repo_files())
    assert scenario.producer["results"][0]["category"] == "preflight_scope_pr_review_only"

    observation = _run_scenario_vc(scenario, tmp_path / "fx")
    assert observation.kind == "pass" and observation.executed, observation.output
    report = _report_from_observation(scenario, observation)
    ws = _executed_ws(tmp_path / "ws", scenario)
    ws.dir.mkdir()

    rc, payload = _adjudicate_executed(ws, report)

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["seq"] == 1
    assert payload["adjudication"]["errors"] == [] and payload["adjudication"]["blocking"] is False
    stored = ws.state()["vc_adjudication"][ws.binding_key()]
    assert [entry["reason_code"] for entry in stored["per_ac"]] == ["pr_review_only_runtime_evidence_pass"]
    _approve_and_assert(ws, payload)


def test_executed_fixture_deferred_canonical_scope_but_uncollected_exit5_is_never_pass(tmp_path, canonical_uncollected):
    """AC5(c)/AC6: canonical annotation but the DECLARED selector collects nothing (pytest exit 5) -> the
    observed exit code is transcribed, nothing is converted to PASS, and the reviewer is not dispatched."""
    observation = _run_scenario_vc(canonical_uncollected, tmp_path / "fx")
    assert observation.exit_code == 5 and observation.kind == "uncollected" and not observation.executed
    report = _report_from_observation(canonical_uncollected, observation)
    assert report["result"] == "FAIL"
    assert report["runtime_ac_results"][0]["exit_code"] == 5
    ws = _executed_ws(tmp_path / "ws", canonical_uncollected)
    ws.dir.mkdir()

    rc, payload = _adjudicate_executed(ws, report)

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(payload)
    assert payload["adjudication"]["overall_status"] == "indeterminate"


def test_executed_fixture_deferred_canonical_scope_but_not_executed_is_never_pass(tmp_path, canonical_pr_review_only):
    """AC5(c): canonical annotation but no executed row at all (the VC never ran) -> not converted to PASS."""
    ws = _executed_ws(tmp_path / "ws", canonical_pr_review_only)
    ws.dir.mkdir()
    report = _report(canonical_pr_review_only, drop=(0,), result="PASS")

    rc, payload = _adjudicate_executed(ws, report)

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_coverage_mismatch"])
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(payload)


def test_executed_fixture_deferred_canonical_scope_with_real_red_exit1_is_fail_closed(tmp_path, canonical_red):
    """AC6: a real assertion failure (exit 1, RED) is transcribed as a failure and also blocks dispatch --
    and it is distinguishable from the exit-5 case by the observed exit code."""
    observation = _run_scenario_vc(canonical_red, tmp_path / "fx")
    assert observation.exit_code == 1 and observation.kind == "red" and observation.executed
    report = _report_from_observation(canonical_red, observation)
    ws = _executed_ws(tmp_path / "ws", canonical_red)
    ws.dir.mkdir()

    rc, payload = _adjudicate_executed(ws, report)

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])
    assert report["runtime_ac_results"][0]["exit_code"] == 1


# --- Issue #3033: lossless Step 2 handoff (child final result -> Step 2 conversion -> step4-adjudicate) ----------
#
# These tests drive the REAL handoff path: a child's full final result as RAW TEXT is written untouched, the
# conversion code block documented in ``step-2-verification.md`` is EXTRACTED FROM THAT DOCUMENT and executed
# unmodified, and its output file is what the real ``step4-adjudicate`` / ``step5-terminal-gate`` CLIs consume.
# The synthetic reports below are test inputs to the handoff, NOT evidence that any real VC was executed
# (the real-session readback comparison stays a deferred runtime verification of Issue #3033).

STEP2_CONVERSION_MARKER = "<!-- step2-handoff-conversion-example -->"


def _step2_conversion_code() -> str:
    text = STEP2_DOC.read_text(encoding="utf-8")
    assert text.count(STEP2_CONVERSION_MARKER) == 1, "exactly one conversion example marker is required"
    match = re.search(re.escape(STEP2_CONVERSION_MARKER) + r"\s*```python\n(.*?)```", text, re.S)
    assert match is not None, "the python code block must directly follow the conversion example marker"
    return match.group(1)


def _raw_final_result(report: dict[str, Any], *, prose: bool = True) -> str:
    """A child's final result as raw text: Japanese prose around ONE fenced TEST_VERDICT yaml block."""
    block = yaml.safe_dump({"TEST_VERDICT": report}, sort_keys=False, allow_unicode=True)
    body = f"```yaml\n{block}```\n"
    if not prose:
        return body
    return f"全 Verification Command を実行しました。結果は次のとおりです。\n\n{body}\n以上が最終報告です。\n"


def _run_step2_handoff(tmp_path: Path, raw_text: str, tag: str) -> tuple[int, Path, str]:
    """Write the raw final result untouched, then run the DOCUMENTED conversion code on it."""
    raw_path = tmp_path / f"test_runner_final_{tag}.txt"
    raw_path.write_text(raw_text, encoding="utf-8")
    out_path = tmp_path / f"test_verdict_{tag}.json"
    completed = subprocess.run(
        [sys.executable, "-", str(raw_path), str(out_path)],
        input=_step2_conversion_code(),
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode, out_path, completed.stderr


def _assert_same_value_and_type(actual: Any, expected: Any, path: str = "$") -> None:
    """Strict equality: key order, list order, exact type (bool is not int), and value."""
    assert type(actual) is type(expected), f"{path}: type {type(actual).__name__} != {type(expected).__name__}"
    if isinstance(expected, dict):
        assert list(actual) == list(expected), f"{path}: keys/order {list(actual)} != {list(expected)}"
        for key in expected:
            _assert_same_value_and_type(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), f"{path}: length {len(actual)} != {len(expected)}"
        for index, (left, right) in enumerate(zip(actual, expected)):
            _assert_same_value_and_type(left, right, f"{path}[{index}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def _handoff_ok(tmp_path: Path, report: dict[str, Any], tag: str) -> Path:
    """Convert ``report`` through the documented handoff, assert it is lossless, return the attempt file."""
    rc, out_path, stderr = _run_step2_handoff(tmp_path, _raw_final_result(report), tag)
    assert rc == 0, stderr
    _assert_same_value_and_type(json.loads(out_path.read_text(encoding="utf-8")), report)
    return out_path


def test_step2_lossless_handoff_pass_report_preserved_and_accepted(tmp_path, multi):
    ws = Workspace(tmp_path, multi)
    report = _report(
        multi,
        row_overrides={
            0: {"test_count": {"subject": "tests/test_a.py -k handoff", "passed": 7}},
            2: {"notes": '日本語の notes と "quote" を含む', "test_count": {"subject": "tests/b.py", "passed": 0}},
        },
        optional_top_level_field={"nested": [1, True, None, "x"]},
    )
    assert [row["ac"] for row in report["runtime_ac_results"]] == ["AC1", "AC1", "AC_UNKNOWN", "AC2,AC3"]

    out_path = _handoff_ok(tmp_path, report, "pass")
    handoff_bytes = out_path.read_bytes()
    loaded = json.loads(handoff_bytes)
    # Spot-check the facts that must never be re-created by the root.
    assert loaded["generated_at"] == GENERATED_AT and loaded["result"] == "PASS"
    assert [row["command_hash"] for row in loaded["runtime_ac_results"]] == multi.hashes
    assert loaded["runtime_ac_results"][0]["test_count"] == {"subject": "tests/test_a.py -k handoff", "passed": 7}
    exit_code = loaded["runtime_ac_results"][1]["exit_code"]
    assert exit_code == 0 and type(exit_code) is int
    assert loaded["runtime_ac_results"][1]["fallback_detected"] is False

    rc, payload = ws.adjudicate(report_path=str(out_path))

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["seq"] == 1 and payload["reason_code"] is None
    assert out_path.read_bytes() == handoff_bytes  # the consumer never rewrites the handoff file
    _approve_and_assert(ws, payload)


@pytest.mark.parametrize("case", sorted(NON_PASS_ROWS), ids=sorted(NON_PASS_ROWS))
def test_step2_lossless_handoff_nonpass_rows_preserved_and_rejected(tmp_path, single, case):
    ws = Workspace(tmp_path, single)
    report = _report(single, row_overrides={0: NON_PASS_ROWS[case]}, result="FAIL")

    out_path = _handoff_ok(tmp_path, report, case)
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    # The failure facts survive the conversion verbatim; nothing is turned into PASS.
    for key, value in NON_PASS_ROWS[case].items():
        assert loaded["runtime_ac_results"][0][key] == value
    assert loaded["result"] == "FAIL"

    rc, payload = ws.adjudicate(report_path=str(out_path))

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] != "approved", term


def test_step2_lossless_handoff_missing_row_not_synthesized(tmp_path, multi):
    ws = Workspace(tmp_path, multi)
    report = _report(multi, drop=(1,))
    assert len(report["runtime_ac_results"]) == len(multi.commands) - 1

    out_path = _handoff_ok(tmp_path, report, "missing_row")
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    # The converter neither re-creates the dropped row from VC metadata nor from the aggregate count.
    assert len(loaded["runtime_ac_results"]) == len(multi.commands) - 1
    assert multi.hashes[1] not in [row["command_hash"] for row in loaded["runtime_ac_results"]]

    rc, payload = ws.adjudicate(report_path=str(out_path))

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert payload["adjudication"]["errors"] and payload["adjudication"]["blocking"] is True
    assert "dispatch" not in ws.state()


def test_step2_lossless_handoff_aggregate_contradiction_not_repaired(tmp_path, single):
    ws = Workspace(tmp_path, single)
    # The row says FAIL while the aggregate counters and the final result claim an all-pass run.
    report = _report(single, row_overrides={0: {"exit_code": 1, "status": "fail"}}, result="PASS")
    assert report["verification_commands_pass"] == 1 and report["verification_commands_fail"] == 0

    out_path = _handoff_ok(tmp_path, report, "contradiction")
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    # The contradiction is carried over as-is: the aggregate is NOT used to rewrite the row (nor vice versa).
    assert loaded["runtime_ac_results"][0]["status"] == "fail" and loaded["runtime_ac_results"][0]["exit_code"] == 1
    assert loaded["verification_commands_pass"] == 1 and loaded["verification_commands_fail"] == 0
    assert loaded["result"] == "PASS"

    rc, payload = ws.adjudicate(report_path=str(out_path))

    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])


def test_step2_lossless_handoff_unquoted_timestamp_is_handoff_failure(tmp_path, single):
    raw = _raw_final_result(_report(single))
    quoted = f"generated_at: '{GENERATED_AT}'"
    assert quoted in raw, "safe_dump is expected to quote the RFC 3339 string"
    unquoted_raw = raw.replace(quoted, f"generated_at: {GENERATED_AT}")
    assert unquoted_raw != raw
    # Sanity: YAML really resolves the unquoted value to a non-JSON type, so only a correction could make it pass.
    block = re.search(r"```yaml\n(.*?)```", unquoted_raw, re.S).group(1)  # type: ignore[union-attr]
    assert not isinstance(yaml.safe_load(block)["TEST_VERDICT"]["generated_at"], str)

    rc, out_path, stderr = _run_step2_handoff(tmp_path, unquoted_raw, "unquoted_ts")

    assert rc != 0
    assert not out_path.exists(), "a failed handoff must leave no output file behind"
    # The known-field type check may fire before JSON encoding, so only the handoff-failure marker is pinned.
    assert "handoff failed" in stderr

    # A stale file at the SAME path is removed too, so it can never be mistaken for this attempt's output.
    raw_path = tmp_path / "test_runner_final_unquoted_ts.txt"
    out_path.write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-", str(raw_path), str(out_path)],
        input=_step2_conversion_code(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0 and not out_path.exists()


@pytest.mark.parametrize("shape", ["no_block", "other_yaml_only", "two_blocks", "not_a_mapping"])
def test_step2_lossless_handoff_ambiguous_raw_result_fails_closed(tmp_path, single, shape):
    good = _raw_final_result(_report(single), prose=False)
    raws = {
        "no_block": "テストを実行しましたが、機械可読な report は添付していません。\n",
        "other_yaml_only": "```yaml\nOTHER:\n  a: 1\n```\n",
        "two_blocks": good + "\n" + good,
        "not_a_mapping": "```yaml\nTEST_VERDICT:\n  - 1\n  - 2\n```\n",
    }

    rc, out_path, stderr = _run_step2_handoff(tmp_path, raws[shape], shape)

    assert rc != 0
    assert not out_path.exists()
    assert "handoff failed" in stderr


def test_step2_handoff_failure_invalidates_prior_pass_and_recovers(tmp_path, single):
    ws = Workspace(tmp_path, single)
    good_report = _report(single)

    # (i) a good handoff stores a PASS and records dispatch seq 1.
    first = _handoff_ok(tmp_path, good_report, "i0_a0")
    rc, payload = ws.adjudicate(report_path=str(first), tag="a0")
    assert rc == 0 and payload["seq"] == 1, payload
    assert ws.binding_key() in ws.state()["vc_adjudication"]
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0 and term["route"] == "approved", term

    # (ii) a NEW handoff attempt fails to convert. Its (never created) attempt path goes to the ordinary
    # step4-adjudicate (no --reuse-stored): the earlier PASS under the same binding is invalidated.
    quoted_raw = _raw_final_result(good_report)
    broken_raw = quoted_raw.replace(f"generated_at: '{GENERATED_AT}'", f"generated_at: {GENERATED_AT}")
    assert broken_raw != quoted_raw
    rc, failed_out, stderr = _run_step2_handoff(tmp_path, broken_raw, "i0_a1")
    assert rc != 0 and not failed_out.exists() and "handoff failed" in stderr
    assert failed_out != first

    rc, payload = ws.adjudicate(report_path=str(failed_out), tag="a1")

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert payload["adjudication"]["errors"] == [f"input_file_not_found:{failed_out}"]
    assert ws.binding_key() not in ws.state().get("vc_adjudication", {})

    # (iii) neither the old dispatch seq nor a read-only step4-gate rerun can use the invalidated PASS.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] != "approved", term
    assert term["reason_code"] == "vc_gate_blocking"
    rc, gate = ws.run(["step4-gate", *ws.binding_args(tag="gate")])
    assert rc == 1 and gate["invoke_pr_reviewer"] is False, gate

    # (iv) recovery: a correct report through a fresh attempt path and an ordinary adjudication.
    recovered = _handoff_ok(tmp_path, good_report, "i0_a2")
    assert recovered not in (first, failed_out)
    rc, payload = ws.adjudicate(report_path=str(recovered), tag="a2")
    assert rc == 0 and payload["invoke_pr_reviewer"] is True, payload
    assert payload["seq"] == 2
    assert ws.binding_key() in ws.state()["vc_adjudication"]
    rc, term = ws.terminal_gate(dispatch_seq=2)
    assert rc == 0 and term["route"] == "approved", term
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] != "approved" and term["reason_code"] == "dispatch_seq_mismatch", term


def test_step2_handoff_nonpass_delegation_keeps_facts_and_is_not_terminal_approval(tmp_path, single):
    delegate = ("--delegate-pr-review-only-nonpass",)
    fail_row = {"exit_code": 1, "status": "fail"}
    ws = Workspace(tmp_path, single)
    # A delegable report carries a non-pass report-level result next to the failing row (the existing #2916 rule).
    report = _report(single, row_overrides={0: fail_row}, result="FAIL")
    out_path = _handoff_ok(tmp_path, report, "fail_row")
    loaded_row = json.loads(out_path.read_text(encoding="utf-8"))["runtime_ac_results"][0]
    assert (loaded_row["exit_code"], loaded_row["status"]) == (1, "fail")  # the conversion kept the FAIL fact

    # (a) default: an executed non-pass is rejected, no dispatch.
    rc, payload = ws.adjudicate(report_path=str(out_path), tag="default")
    _assert_fail_closed_no_dispatch(ws, rc, payload, ["pr_review_only_current_execution_not_pass:AC1"])

    # (b) the existing opt-in delegates the SAME handoff file, still as a failure fact (not PASS).
    rc, payload = ws.adjudicate(report_path=str(out_path), extra_args=delegate, tag="delegate")
    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["seq"] == 1 and payload["reason_code"] is None
    assert payload["adjudication"]["overall_status"] == "indeterminate"
    assert payload["adjudication"]["blocking"] is True
    assert payload["adjudication"]["pr_review_only_nonpass_delegated"] == ["AC1"]
    stored = ws.state()["vc_adjudication"][ws.binding_key()]
    assert stored["overall_status"] == "indeterminate" and stored["blocking"] is True
    entry = stored["per_ac"][0]
    assert entry["status"] == "indeterminate" and entry["blocking"] is True
    assert entry["reason_code"] == "pr_review_only_nonpass_delegated_to_reviewer"
    facts = {row["key"].partition("=")[0]: json.loads(row["key"].partition("=")[2]) for row in entry["failure_keys"]}
    assert facts["exit_code"] == 1 and facts["status"] == "fail"
    assert "pr_review_only_runtime_evidence_pass" not in json.dumps(stored)

    # (c) dispatch permission is not terminal approval: a reviewer that has not approved never reaches it.
    for verdict in ("REQUEST_CHANGES", "PENDING"):
        rc, term = ws.terminal_gate(dispatch_seq=1, verdict=_reviewer_verdict(single.head, verdict, ["AC1 failed"]))
        assert rc == 1 and term["route"] != "approved", (verdict, term)
    # Only the reviewer's own APPROVE through step5-terminal-gate approves; the stored adjudication stays a non-pass.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0 and term["route"] == "approved", term
    stored = ws.state()["vc_adjudication"][ws.binding_key()]
    assert stored["overall_status"] == "indeterminate" and stored["blocking"] is True


@pytest.mark.parametrize(
    "case",
    [
        "missing_row",
        "head_binding_mismatch",
        "incoherent_pass_with_fallback",
        "trust_marker_present",
        "report_pass_over_failing_row",
    ],
)
def test_step2_handoff_nonpass_delegation_invalid_report_stays_fail_closed(tmp_path, multi, case):
    ws = Workspace(tmp_path, multi)
    fail_row = {0: {"exit_code": 1, "status": "fail"}}
    reports = {
        "missing_row": _report(multi, row_overrides=fail_row, drop=(3,), result="FAIL"),
        "head_binding_mismatch": _report(multi, row_overrides=fail_row, head="9" * 40, result="FAIL"),
        # status pass + fallback true is an incoherent fact set, never a delegable failure.
        "incoherent_pass_with_fallback": _report(multi, row_overrides={0: {"fallback_detected": True}}, result="FAIL"),
        "trust_marker_present": _report(multi, row_overrides=fail_row, workflow_run_id=None, result="FAIL"),
        # A report-level PASS dressed over a failing row is a contradiction, not a delegable failure.
        "report_pass_over_failing_row": _report(multi, row_overrides=fail_row, result="PASS"),
    }
    out_path = _handoff_ok(tmp_path, reports[case], case)

    rc, payload = ws.adjudicate(report_path=str(out_path), extra_args=("--delegate-pr-review-only-nonpass",))

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None, payload
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    expected_errors = {
        "missing_row": ["pr_review_only_coverage_mismatch"],
        "head_binding_mismatch": ["pr_review_only_head_binding_mismatch"],
        "incoherent_pass_with_fallback": ["pr_review_only_current_execution_not_pass:AC1"],
        "trust_marker_present": ["pr_review_only_current_authorization_mismatch:AC1"],
        "report_pass_over_failing_row": ["pr_review_only_nonpass_report_result_inconsistent"],
    }
    assert payload["adjudication"]["errors"] == expected_errors[case], payload
    state = ws.state()
    assert "dispatch" not in state and state.get("vc_adjudication", {}) == {}


# --- Issue #3033 (OWNER fix_delta): raw-YAML information loss must fail closed -------------------------------------
#
# Duplicate mapping keys, Markdown fence mis-detection and YAML 1.1 implicit type conversion all lose information
# at PARSE time, so a parsed-object-vs-JSON comparison cannot see them. Every negative input below is therefore
# LITERAL raw YAML text (never ``yaml.safe_dump`` output, which can only emit what was already preserved), and
# the conversion is always the code block extracted from ``step-2-verification.md`` (nothing is re-implemented).

FENCE = "`" * 3
LONG_FENCE = "`" * 4


def _literal_row(
    scenario: Scenario,
    *,
    command: str | None = None,
    exit_code: str = "0",
    status: str = "pass",
    fallback: str = "false",
    notes: str = "''",
    extra: str = "",
) -> str:
    """One ``runtime_ac_results`` list item as LITERAL YAML (same key order as ``_row``); values are raw YAML."""
    first = scenario.commands[0]
    assert "'" not in first["raw_command"]
    command_text = command if command is not None else f"'{first['raw_command']}'"
    return (
        f"  - ac: {first['ac']}\n"
        f"    command: {command_text}\n"
        f"    command_hash: '{first['command_hash']}'\n"
        f"    exit_code: {exit_code}\n"
        f"    status: {status}\n"
        f"    fallback_detected: {fallback}\n"
        "    artifact_present: not_required\n"
        "    human_review_required: false\n"
        "    stop_condition_triggered: false\n"
        f"    notes: {notes}\n"
        f"{extra}"
    )


def _literal_report(
    scenario: Scenario,
    *,
    generated_at: str = f"'{GENERATED_AT}'",
    result: str = "PASS",
    rows: str | None = None,
    extra: str = "",
) -> str:
    """A full ``TEST_VERDICT`` mapping as LITERAL YAML (same key order as ``_report(descriptive=False)``)."""
    rows_text = rows if rows is not None else "  runtime_ac_results:\n" + _literal_row(scenario)
    return (
        "TEST_VERDICT:\n"
        "  schema: TEST_VERDICT_MACHINE/v2\n"
        f"  issue_number: {ISSUE_NUMBER}\n"
        f"  pr_number: {PR_NUMBER}\n"
        f"  head_sha: '{scenario.head}'\n"
        f"  reviewed_head_sha: '{scenario.head}'\n"
        f"  diff_head_sha: '{scenario.head}'\n"
        f"  contract_body_sha256: '{scenario.body_sha256}'\n"
        f"  generated_at: {generated_at}\n"
        f"  result: {result}\n"
        f"{rows_text}{extra}"
    )


def _fenced(yaml_text: str, *, opening: str = FENCE, closing: str | None = None, lang: str = "yaml") -> str:
    """Japanese prose around ONE fenced block; ``closing`` defaults to ``opening`` (pass ``""`` to omit it)."""
    closing_line = opening if closing is None else closing
    tail = f"{closing_line}\n" if closing_line else ""
    return f"全 Verification Command を実行しました。\n\n{opening}{lang}\n{yaml_text}{tail}\n以上が最終報告です。\n"


def _assert_handoff_rejected(rc: int, out_path: Path, stderr: str, needle: str) -> None:
    assert rc != 0, "the handoff must fail closed"
    assert not out_path.exists(), "a failed handoff must leave no output file behind"
    assert "handoff failed" in stderr and needle in stderr, stderr


def _assert_literal_handoff_lossless(tmp_path: Path, raw_text: str, expected: dict[str, Any], tag: str) -> Path:
    rc, out_path, stderr = _run_step2_handoff(tmp_path, raw_text, tag)
    assert rc == 0, stderr
    _assert_same_value_and_type(json.loads(out_path.read_text(encoding="utf-8")), expected)
    return out_path


def _duplicate_key_cases(scenario: Scenario) -> dict[str, str]:
    """Raw YAML in which a LATER duplicate key would (under plain ``safe_load``) silently erase an EARLIER fact."""
    fail_row = _literal_row(scenario, status="fail", exit_code="1")
    pass_row = _literal_row(scenario)
    return {
        "top_level_result": _literal_report(scenario, result="FAIL", extra="  result: PASS\n"),
        "top_level_runtime_ac_results": _literal_report(
            scenario, rows="  runtime_ac_results:\n" + fail_row, extra="  runtime_ac_results:\n" + pass_row
        ),
        "row_status": _literal_report(
            scenario,
            rows="  runtime_ac_results:\n" + _literal_row(scenario, status="fail", extra="    status: pass\n"),
        ),
        "row_exit_code": _literal_report(
            scenario,
            rows="  runtime_ac_results:\n" + _literal_row(scenario, exit_code="1", extra="    exit_code: 0\n"),
        ),
        "nested_optional_field": _literal_report(
            scenario,
            rows="  runtime_ac_results:\n"
            + _literal_row(scenario, extra="    test_count:\n      passed: 0\n      passed: 7\n"),
        ),
    }


@pytest.mark.parametrize(
    "case", ["top_level_result", "top_level_runtime_ac_results", "row_status", "row_exit_code", "nested_optional_field"]
)
def test_step2_lossless_handoff_duplicate_mapping_key_is_handoff_failure(tmp_path, single, case):
    yaml_text = _duplicate_key_cases(single)[case]
    # Sanity: plain safe_load really accepts this text and silently keeps only the LAST duplicate, so only an
    # explicit duplicate check can make the handoff fail.
    assert isinstance(yaml.safe_load(yaml_text)["TEST_VERDICT"], dict)

    rc, out_path, stderr = _run_step2_handoff(tmp_path, _fenced(yaml_text), case)

    _assert_handoff_rejected(rc, out_path, stderr, "duplicate mapping key")


def test_step2_lossless_handoff_duplicate_key_failure_removes_stale_valid_output(tmp_path, single):
    good_out = _handoff_ok(tmp_path, _report(single, descriptive=False), "stale")
    assert good_out.exists() and json.loads(good_out.read_text(encoding="utf-8"))["result"] == "PASS"

    # Same attempt path, but the raw result now has a duplicate key: the earlier valid output must not survive.
    rc, out_path, stderr = _run_step2_handoff(tmp_path, _fenced(_duplicate_key_cases(single)["row_status"]), "stale")

    assert out_path == good_out
    _assert_handoff_rejected(rc, out_path, stderr, "duplicate mapping key")


BACKTICK_COMMAND = "rg -n '```' README.md"
BACKTICK_NOTES = "first line\n```\n```yaml\nlast line"
BACKTICK_ROW_YAML = {
    "command": f'"{BACKTICK_COMMAND}"',
    "notes": "|-\n      first line\n      ```\n      ```yaml\n      last line",
}


def _backtick_report_yaml(scenario: Scenario) -> str:
    return _literal_report(
        scenario,
        rows="  runtime_ac_results:\n"
        + _literal_row(scenario, command=BACKTICK_ROW_YAML["command"], notes=BACKTICK_ROW_YAML["notes"]),
    )


def _backtick_report(scenario: Scenario) -> dict[str, Any]:
    report = _report(scenario, descriptive=False, row_overrides={0: {"notes": BACKTICK_NOTES}})
    # ``_row`` takes the command as a positional argument, so the verbatim command is set on the built row.
    report["runtime_ac_results"][0]["command"] = BACKTICK_COMMAND
    return report


def test_step2_lossless_handoff_triple_backticks_in_command_and_notes_reach_adjudicate(tmp_path, single):
    ws = Workspace(tmp_path, single)
    expected = _backtick_report(single)
    raw = _fenced(_backtick_report_yaml(single))
    # Sanity: the old non-greedy regex fence scan would have cut the block at the first backticks of ``command``.
    old_blocks = [
        block
        for block in re.findall(FENCE + r"ya?ml[ \t]*\n(.*?)" + FENCE, raw, re.S)
        if re.search(r"^TEST_VERDICT:", block, re.M)
    ]
    assert len(old_blocks) == 1 and "last line" not in old_blocks[0]

    out_path = _assert_literal_handoff_lossless(tmp_path, raw, expected, "backticks")
    loaded_row = json.loads(out_path.read_text(encoding="utf-8"))["runtime_ac_results"][0]
    assert loaded_row["command"] == BACKTICK_COMMAND and loaded_row["notes"] == BACKTICK_NOTES

    rc, payload = ws.adjudicate(report_path=str(out_path))

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["seq"] == 1 and payload["reason_code"] is None
    _approve_and_assert(ws, payload)


def test_step2_lossless_handoff_four_backtick_fence_containing_triple_backtick_lines_is_accepted(tmp_path, single):
    raw = _fenced(_backtick_report_yaml(single), opening=LONG_FENCE)

    _assert_literal_handoff_lossless(tmp_path, raw, _backtick_report(single), "long_fence")


@pytest.mark.parametrize("shape", ["no_fence", "unterminated_fence", "closing_shorter_than_opening", "two_candidates"])
def test_step2_lossless_handoff_fence_shape_fails_closed(tmp_path, single, shape):
    yaml_text = _backtick_report_yaml(single)
    raws = {
        "no_fence": f"報告です。\n\n{yaml_text}\n以上。\n",
        "unterminated_fence": _fenced(yaml_text, closing=""),
        # A 4-backtick opening must NOT be closed by a 3-backtick line.
        "closing_shorter_than_opening": _fenced(yaml_text, opening=LONG_FENCE, closing=FENCE),
        "two_candidates": _fenced(yaml_text) + "\n" + _fenced(_literal_report(single)),
    }
    needles = {
        "no_fence": "found 0",
        "unterminated_fence": "unterminated markdown fence",
        "closing_shorter_than_opening": "unterminated markdown fence",
        "two_candidates": "found 2",
    }

    rc, out_path, stderr = _run_step2_handoff(tmp_path, raws[shape], shape)

    _assert_handoff_rejected(rc, out_path, stderr, needles[shape])


@pytest.mark.parametrize("value", ["off", "on", "yes", "no"])
def test_step2_lossless_handoff_unquoted_boolean_like_scalar_is_rejected(tmp_path, single, value):
    raw = _fenced(_literal_report(single, rows="  runtime_ac_results:\n" + _literal_row(single, notes=value)))
    # Sanity: PyYAML resolves it to a bool, so without the strict constructor the intent (the word) is lost.
    assert yaml.safe_load(raw.split("yaml\n", 1)[1].split(FENCE, 1)[0])["TEST_VERDICT"]["runtime_ac_results"][0][
        "notes"
    ] in (True, False)

    rc, out_path, stderr = _run_step2_handoff(tmp_path, raw, f"bool_{value}")

    _assert_handoff_rejected(rc, out_path, stderr, "ambiguous unquoted boolean-like scalar")


@pytest.mark.parametrize(
    ("label", "row_kwargs", "needle"),
    [
        ("octal_notes", {"notes": "010"}, "ambiguous unquoted numeric scalar"),
        ("underscore_notes", {"notes": "1_000"}, "ambiguous unquoted numeric scalar"),
        ("sexagesimal_field", {"extra": "    x: 1:30\n"}, "ambiguous unquoted numeric scalar"),
        ("infinity_field", {"extra": "    x: .inf\n"}, "ambiguous unquoted numeric scalar"),
        ("integer_mapping_key", {"extra": "    test_count:\n      1: a\n"}, "non-string mapping key"),
        ("float_mapping_key", {"extra": "    test_count:\n      1.5: a\n"}, "non-string mapping key"),
        ("null_mapping_key", {"extra": "    test_count:\n      null: a\n"}, "non-string mapping key"),
        ("boolean_mapping_key", {"extra": "    test_count:\n      on: a\n"}, "ambiguous unquoted boolean-like scalar"),
        # A known bool field written as ``no`` / ``yes`` would silently pass the type check as a real bool.
        ("fallback_detected_no", {"fallback": "no"}, "ambiguous unquoted boolean-like scalar"),
        ("fallback_detected_yes", {"fallback": "yes"}, "ambiguous unquoted boolean-like scalar"),
        ("optional_nested_off", {"extra": "    test_count:\n      flag: off\n"}, "ambiguous unquoted boolean-like"),
        ("exit_code_bool", {"exit_code": "true"}, "exit_code must be int, got bool"),
        ("exit_code_string", {"exit_code": "'0'"}, "exit_code must be int, got str"),
        ("exit_code_float", {"exit_code": "0.0"}, "exit_code must be int, got float"),
        ("fallback_detected_string", {"fallback": "'false'"}, "fallback_detected must be bool, got str"),
        ("fallback_detected_int", {"fallback": "0"}, "fallback_detected must be bool, got int"),
        ("status_bool", {"status": "true"}, "status must be str, got bool"),
    ],
)
def test_step2_lossless_handoff_ambiguous_scalar_or_key_is_rejected(tmp_path, single, label, row_kwargs, needle):
    raw = _fenced(_literal_report(single, rows="  runtime_ac_results:\n" + _literal_row(single, **row_kwargs)))

    rc, out_path, stderr = _run_step2_handoff(tmp_path, raw, label)

    _assert_handoff_rejected(rc, out_path, stderr, needle)


@pytest.mark.parametrize(
    ("label", "report_kwargs", "needle"),
    [
        ("result_bool", {"result": "true"}, "result must be a string"),
        ("result_null", {"result": "null"}, "result must be a string"),
        ("top_level_boolean_like", {"extra": "  flag: off\n"}, "ambiguous unquoted boolean-like scalar"),
    ],
)
def test_step2_lossless_handoff_top_level_type_violation_is_rejected(tmp_path, single, label, report_kwargs, needle):
    raw = _fenced(_literal_report(single, **report_kwargs))

    rc, out_path, stderr = _run_step2_handoff(tmp_path, raw, label)

    _assert_handoff_rejected(rc, out_path, stderr, needle)


@pytest.mark.parametrize("word", ["off", "on", "yes", "no", "010", "1:30", "1_000"])
def test_step2_lossless_handoff_quoted_ambiguous_strings_and_optional_fields_are_preserved(tmp_path, single, word):
    ws = Workspace(tmp_path, single)
    test_count = {
        "subject": "tests/test_a.py -k handoff",
        "passed": 7,
        "failed": 0,
        "ratio": 0.5,
        "skipped": None,
        "labels": [word, True, False, None, 3],
    }
    expected = _report(
        single,
        descriptive=False,
        row_overrides={0: {"notes": word, "test_count": test_count}},
        optional_note=word,
    )
    row_extra = (
        "    test_count:\n"
        "      subject: tests/test_a.py -k handoff\n"
        "      passed: 7\n"
        "      failed: 0\n"
        "      ratio: 0.5\n"
        "      skipped: null\n"
        f'      labels: ["{word}", true, false, null, 3]\n'
    )
    raw = _fenced(
        _literal_report(
            single,
            rows="  runtime_ac_results:\n" + _literal_row(single, notes=f'"{word}"', extra=row_extra),
            extra=f"  optional_note: '{word}'\n",
        )
    )

    out_path = _assert_literal_handoff_lossless(tmp_path, raw, expected, f"quoted_{word.replace(':', '_')}")
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    row = loaded["runtime_ac_results"][0]
    assert row["notes"] == word and type(row["notes"]) is str
    assert row["fallback_detected"] is False and row["human_review_required"] is False
    assert row["exit_code"] == 0 and type(row["exit_code"]) is int
    assert row["test_count"] == test_count and loaded["optional_note"] == word

    rc, payload = ws.adjudicate(report_path=str(out_path))

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["seq"] == 1
    _approve_and_assert(ws, payload)


@pytest.mark.parametrize("case", ["duplicate_status", "ambiguous_boolean_notes"])
def test_step2_handoff_duplicate_or_ambiguous_failure_invalidates_prior_pass(tmp_path, single, case):
    ws = Workspace(tmp_path, single)

    # (i) a good handoff stores a PASS and records dispatch seq 1.
    first = _handoff_ok(tmp_path, _report(single), "inv_a0")
    rc, payload = ws.adjudicate(report_path=str(first), tag="a0")
    assert rc == 0 and payload["seq"] == 1, payload
    assert ws.binding_key() in ws.state()["vc_adjudication"]

    # (ii) a new attempt whose raw result loses information fails to convert; its never-created path goes to the
    # ordinary step4-adjudicate and the earlier PASS under the same binding is invalidated.
    broken = {
        "duplicate_status": _duplicate_key_cases(single)["row_status"],
        "ambiguous_boolean_notes": _literal_report(
            single, rows="  runtime_ac_results:\n" + _literal_row(single, notes="off")
        ),
    }[case]
    rc, failed_out, stderr = _run_step2_handoff(tmp_path, _fenced(broken), "inv_a1")
    assert rc != 0 and not failed_out.exists() and "handoff failed" in stderr
    assert failed_out != first

    rc, payload = ws.adjudicate(report_path=str(failed_out), tag="a1")

    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert payload["adjudication"]["errors"] == [f"input_file_not_found:{failed_out}"]
    assert ws.binding_key() not in ws.state().get("vc_adjudication", {})
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["route"] != "approved", term
