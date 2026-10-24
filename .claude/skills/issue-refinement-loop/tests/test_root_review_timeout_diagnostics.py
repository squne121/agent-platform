"""Issue #2897 AC3/AC4 (root review pipeline): the bounded
`timeout_diagnostics` of a command-level VC timeout survives the temporary
readiness artifact cleanup and is retrievable from BOTH the verified
transport artifact (`REVIEWER_COMPACT_ARTIFACT_V2` `semantic_result`, the
canonical readback input) and the full review artifact.

Production path exercised by every test here, end to end:

    fake `baseline_vc_preflight.run_command()` return value (permitted seam 1:
    timeout sentinel `exit_code == -1` and `stderr == "timeout"`)
      -> REAL `baseline_vc_preflight.main()` result builder
      -> REAL `contract_readiness_check.main()` readiness conversion
      -> REAL `check_issue_contract.main()` review check and
         `--mode merge_readiness` merge
      -> REAL `run_root_review_pipeline.run_checker_pipeline_once()`
         (temporary-directory cleanup in its `finally`)
      -> REAL `reviewer_transport.run_reviewer_transport()` artifact writer
      -> REAL `run_root_review_pipeline._cmd_produce()` full artifact writer
      -> persisted bytes + `readback_persisted_artifact()` verified readback

Permitted seam 2 (process-launch mechanics only): the places that would spawn
`run-checker-attempt`, `check_issue_contract.py`, `contract_readiness_check.py`
or `baseline_vc_preflight.py` as child processes instead call the SAME
script's `main()` in-process and capture stdout / exit code, so the seam-1
`run_command()` replacement is visible to the "child". The only other
replaced input is the live Issue body fetch (`fetch_and_pin_live_body`, the
same replacement `test_root_review_canonical_delivery.py` uses); it supplies
the pinned body text and nothing about the result.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import signal
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock


_SKILLS = Path(__file__).resolve().parents[2]
for _path in (
    _SKILLS / "issue-refinement-loop" / "scripts",
    _SKILLS / "issue-contract-review" / "scripts",
    _SKILLS / "review-issue" / "scripts",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import baseline_vc_preflight as bvp  # noqa: E402
import check_issue_contract as cic  # noqa: E402
import contract_readiness_check as crc  # noqa: E402
import reviewer_transport as transport  # noqa: E402
import run_root_review_pipeline as pipeline  # noqa: E402
import vc_runtime_history as history  # noqa: E402
from _trusted_repo import TRUSTED_REPO  # noqa: E402

REPO = TRUSTED_REPO

TIMEOUT_OUTCOME = (-1, "", "timeout", 1234, {})
NOT_FOUND_OUTCOME = (4, "", "ERROR: file or directory not found: x", 5, {})
SUCCESS_OUTCOME = (0, "", "", 5, {})

_NEW_TEST_PATH = ".claude/skills/issue-refinement-loop/tests/test_fixture_target_not_yet_created.py"
_PYTEST_VC = f"uv run --locked pytest {_NEW_TEST_PATH}::test_target"
_PURE_VC = "test -f README.md"

_BODY_HEADER = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "root review timeout diagnostics fixture"
change_kind: workflow
```

## Outcome

Fixture for the root review timeout diagnostics.

## Acceptance Criteria

- [ ] AC1: the root artifact keeps the timed-out occurrence identity.
- [ ] AC2: the root artifact never attributes a timeout to another occurrence.

## Verification Commands

"""

_ALLOWED = f"""
## Allowed Paths

- {_NEW_TEST_PATH}
"""

# Call order (pure command, pytest block 1, pytest block 2). The two pytest
# blocks collide on (AC, block-relative line, command_hash).
_TWO_BLOCK_BODY = (
    _BODY_HEADER
    + f"""```bash
# AC1
$ {_PURE_VC}
```

```bash
# AC1
# baseline-expect: fail
$ {_PYTEST_VC}
```

```bash
# AC1
# baseline-expect: fail
$ {_PYTEST_VC}
```
"""
    + _ALLOWED
)

# 20 identical pure commands: one real execution (occurrence 0, timeout) and
# 19 dedup replays that carry the same timeout outcome -> 20 timeout
# occurrences, above the 16-occurrence bound.
_TWENTY_TIMEOUTS_BODY = (
    _BODY_HEADER
    + "".join(f"```bash\n# AC2\n$ {_PURE_VC}\n```\n\n" for _ in range(20))
    + _ALLOWED
)

_APPROVE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "root review timeout diagnostics fixture (approve branch)"
change_kind: research
```

## Outcome

Fixture proving a normal success carries no timeout diagnostics.

## Acceptance Criteria

- [ ] AC1: fixture body is well-formed enough for an approve verdict.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/root_review_timeout_diagnostics_approve.md
"""

_DIAGNOSTIC_TOP_LEVEL_KEYS = {
    "schema_version",
    "body_sha256",
    "canonical_plan_digest",
    "results_count",
    "total_timeout_occurrences",
    "truncated_count",
    "occurrences",
}
_DIAGNOSTIC_OCCURRENCE_KEYS = {
    "attribution",
    "reason_code",
    "occurrence_index",
    "line",
    "line_coordinate",
    "command_hash",
    "execution_key_hash",
    "execution_source",
    "dedup_source_result_index",
    "timeout_provenance",
}


class _Harness:
    """Wires the production pipeline to in-process script launches."""

    def __init__(
        self,
        monkeypatch,
        tmp_path: Path,
        body: str,
        *,
        outcomes_by_call: dict[int, tuple] | None = None,
        default_outcome: tuple = NOT_FOUND_OUTCOME,
        on_transport_launch=None,
        baseline_supervisor_timed_out: bool = False,
    ):
        self.tmp_path = tmp_path
        self.body = body
        self.body_sha256 = pipeline.sha256_of(body)
        self.outcomes_by_call = outcomes_by_call or {}
        self.default_outcome = default_outcome
        self.run_command_calls: list[tuple[str, int]] = []
        self.baseline_payloads: list[dict] = []
        self.readiness_payloads: list[dict] = []
        self.readiness_files_seen: list[str] = []
        self.transport_launches = 0
        self._on_transport_launch = on_transport_launch
        self._baseline_supervisor_timed_out = baseline_supervisor_timed_out

        monkeypatch.setattr(pipeline, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "fetch_and_pin_live_body", self._fake_fetch)
        monkeypatch.setattr(bvp, "run_command", self._fake_run_command)
        monkeypatch.setattr(bvp, "run_subprocess_with_cooperative_supervisor", self._supervisor)
        monkeypatch.setattr(crc, "_run_subprocess_with_cooperative_supervisor", self._supervisor)
        monkeypatch.setattr(
            pipeline,
            "subprocess",
            types.SimpleNamespace(run=self._fake_subprocess_run, TimeoutExpired=subprocess.TimeoutExpired),
        )
        monkeypatch.setattr(
            transport,
            "subprocess",
            types.SimpleNamespace(
                Popen=self._fake_popen,
                DEVNULL=subprocess.DEVNULL,
                PIPE=subprocess.PIPE,
                TimeoutExpired=subprocess.TimeoutExpired,
            ),
        )

    # -- inputs / seam 1 ----------------------------------------------------

    def _fake_fetch(self, issue_number, repo, timeout_seconds=15):
        return self.body, self.body_sha256, None

    def _fake_run_command(self, command: str, timeout_seconds: int, cwd: str):
        index = len(self.run_command_calls)
        self.run_command_calls.append((command, timeout_seconds))
        return self.outcomes_by_call.get(index, self.default_outcome)

    # -- seam 2: launch mechanics -------------------------------------------

    def _run_script(self, argv: list[str]) -> tuple[int, str, str]:
        script = Path(argv[1]).name
        out, err = io.StringIO(), io.StringIO()
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                if script == "run_root_review_pipeline.py":
                    code = pipeline.main(argv[2:])
                else:
                    module_main = {
                        "check_issue_contract.py": cic.main,
                        "contract_readiness_check.py": crc.main,
                        "baseline_vc_preflight.py": bvp.main,
                    }[script]
                    with mock.patch.object(sys, "argv", [argv[1], *argv[2:]]):
                        try:
                            code = module_main() or 0
                        except SystemExit as exc:
                            code = exc.code if isinstance(exc.code, int) else 1
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if script == "baseline_vc_preflight.py":
            self.baseline_payloads.append(json.loads(out.getvalue()))
        if script == "contract_readiness_check.py" and "execute" in argv:
            self.readiness_payloads.append(json.loads(out.getvalue()))
        if script == "check_issue_contract.py" and "merge_readiness" in argv:
            self.readiness_files_seen.append(argv[argv.index("--readiness-result-file") + 1])
        return code, out.getvalue(), err.getvalue()

    def _supervisor(self, argv, *, timeout_seconds, cwd=None, env=None, **_ignored):
        script = Path(argv[1]).name
        if script == "baseline_vc_preflight.py" and self._baseline_supervisor_timed_out:
            # The aggregate wrapper reports its own timeout.
            return bvp.SupervisedSubprocessResult(-1, "", "", True, 0.0)
        code, stdout, stderr = self._run_script(argv)
        return bvp.SupervisedSubprocessResult(code, stdout, stderr, False, 0.0)

    def _fake_subprocess_run(self, cmd, capture_output=True, text=True, timeout=None, **_ignored):
        code, stdout, stderr = self._run_script(list(cmd))
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)

    def _fake_popen(self, command, **_ignored):
        self.transport_launches += 1
        if self._on_transport_launch is not None:
            self._on_transport_launch()
        code, stdout, stderr = self._run_script(list(command))

        class _Process:
            pid = 424242
            returncode = code

            def __init__(self):
                self.stdout = io.BytesIO(stdout.encode("utf-8"))
                self.stderr = io.BytesIO(stderr.encode("utf-8"))

            def wait(self, timeout=None):
                return self.returncode

        return _Process()

    # -- production entrypoint ----------------------------------------------

    def produce(self, issue_number: int) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline._cmd_produce(argparse.Namespace(issue_number=issue_number, repo=REPO))
        return code, json.loads(out.getvalue())

    def verified_readback(self, out: dict, issue_number: int, expected_verdict: str) -> dict:
        vta = out["verified_transport_artifact"]
        return pipeline.readback_persisted_artifact(
            artifact_root=vta["root"],
            artifact_relative=vta["relative_path"],
            expected_repo=REPO,
            expected_issue=issue_number,
            expected_body_sha256=self.body_sha256,
            expected_invocation_id=vta["invocation_id"],
            expected_attempt=vta["attempt"],
            expected_artifact_sha256=vta["sha256"],
            expected_verdict=expected_verdict,
        )


def _persisted_artifacts(out: dict) -> tuple[dict, dict, bytes]:
    """(full review artifact JSON, transport semantic_result, transport bytes)
    read back from disk."""
    full = json.loads(Path(out["full_review_artifact"]["path"]).read_text(encoding="utf-8"))
    vta = out["verified_transport_artifact"]
    transport_bytes = (Path(vta["root"]) / vta["relative_path"]).read_bytes()
    transport_payload = transport.strict_json_loads(transport_bytes)
    return full, transport_payload["semantic_result"], transport_bytes


def _attempt_result(out: dict, issue_number: int) -> dict:
    vta = out["verified_transport_artifact"]
    path = (
        Path(vta["root"])
        / transport.attempt_relative_dir(issue_number, vta["invocation_id"], vta["attempt"])
        / "attempt_result.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_production_path_diagnostic_survives_cleanup_in_transport_and_full_artifact(
    monkeypatch, tmp_path
):
    issue_number = 2897001
    harness = _Harness(
        monkeypatch, tmp_path, _TWO_BLOCK_BODY, outcomes_by_call={2: TIMEOUT_OUTCOME}
    )
    code, out = harness.produce(issue_number)
    assert code == 0 and out["status"] == "ok", out

    # Existing routing facts are unchanged: an inner readiness timeout stays
    # the operator-intervention route, not a transport failure.
    assert out["compact_result"]["verdict"] == "needs-fix"
    assert out["compact_result"]["next_action"] == "request_changes"
    assert out["merged_review_result"]["failure_class"] == "contract_readiness_human_judgment"
    assert out["canonical_step2_route"] == pipeline.STEP_5_OPERATOR_INTERVENTION_REQUIRED
    attempt = _attempt_result(out, issue_number)
    assert attempt["transport_status"] == "ok"
    assert attempt["timeout"] is False and attempt["exit_code"] == 0
    assert attempt["reason_code"] is None  # not misclassified as an outer timeout

    # Temporary readiness artifact (and its scratch directory) are gone.
    assert harness.readiness_files_seen, "merge step did not run"
    for readiness_file in harness.readiness_files_seen:
        assert not Path(readiness_file).exists()
        assert not Path(readiness_file).parent.exists()
    assert list((tmp_path / "tmp").iterdir()) == []

    # Persisted bytes: canonical transport artifact and full review artifact.
    full_artifact, semantic_result, transport_bytes = _persisted_artifacts(out)
    assert b"timeout_diagnostics" in transport_bytes
    diagnostics = semantic_result["timeout_diagnostics"]
    assert full_artifact["timeout_diagnostics"] == diagnostics
    assert out["merged_review_result"]["timeout_diagnostics"] == diagnostics

    # Verified readback (what gate-final-review consumes) returns the same.
    readback = harness.verified_readback(out, issue_number, "needs-fix")
    assert readback["verdict_identity"] is True, readback
    assert readback["payload"]["semantic_result"]["timeout_diagnostics"] == diagnostics
    gate = pipeline.gate_final_review(remote_update_ok=True, readback=readback)
    assert gate["final_review_allowed"] is True

    # The diagnostic ties the plan digest and the applied budget of the
    # timed-out occurrence (index 2) to the REAL preflight result.
    raw = harness.baseline_payloads[0]
    raw_item = raw["results"][2]
    assert diagnostics["body_sha256"] == harness.body_sha256
    assert diagnostics["canonical_plan_digest"] == raw["diagnostic_report"]["canonical_plan_digest"]
    assert diagnostics["total_timeout_occurrences"] == 1
    (occurrence,) = diagnostics["occurrences"]
    assert occurrence["attribution"] == "attributed"
    assert occurrence["occurrence_index"] == 2
    assert occurrence["command_hash"] == raw_item["command_hash"]
    assert occurrence["timeout_provenance"] == raw_item["timeout_provenance"]
    # ...and that applied budget is the timeout run_command() actually got.
    assert harness.run_command_calls[2] == (
        _PYTEST_VC,
        occurrence["timeout_provenance"]["timeout_seconds"],
    )
    # The other pytest block (same hash, same line) is not attributed.
    assert raw["results"][1]["command_hash"] == raw_item["command_hash"]
    assert raw["results"][1]["line"] == raw_item["line"]


def _seed_history(store: Path, command: str, *, duration_ms: int, count: int) -> None:
    resolved_repo_root = bvp.resolve_repo_root_for_history(".")
    group_key = history.compute_command_group_key(command, ".", repo_root=resolved_repo_root)
    fingerprint = history.compute_environment_fingerprint(bvp._command_family(command))
    for _ in range(count):
        outcome = history.record_sample(
            store,
            execution_id=history.new_execution_id(),
            command_group_key=group_key,
            environment_fingerprint=fingerprint,
            status="success",
            command_hash=bvp.compute_command_hash(command),
            duration_ms=duration_ms,
            applied_timeout_ms=150000,
        )
        assert outcome["recorded"], outcome


def _pytest_vc_budget_from_store(body: str) -> dict:
    resolved_repo_root = bvp.resolve_repo_root_for_history(".")
    snapshot = bvp.produce_immutable_history_snapshot(body, cwd=".", repo_root=resolved_repo_root)
    plan = bvp.compute_canonical_vc_plan(
        body,
        cwd=".",
        allowed_paths=bvp.extract_allowed_paths(body),
        history_snapshot=snapshot,
        repo_root=resolved_repo_root,
    )
    command_hash = bvp.compute_command_hash(_PYTEST_VC)
    (budget,) = [b for b in plan["command_budgets"] if b["command_hash"] == command_hash]
    return budget


def test_snapshot_budget_unchanged_after_history_mutation(monkeypatch, tmp_path):
    issue_number = 2897002
    store = tmp_path / "history.sqlite3"
    monkeypatch.setenv("VC_RUNTIME_HISTORY_STORE_PATH", str(store))
    _seed_history(store, _PYTEST_VC, duration_ms=120000, count=5)

    # What the root-owned immutable snapshot (taken once at the start of
    # `produce`) resolves for the timed-out command.
    budget_at_snapshot = _pytest_vc_budget_from_store(_TWO_BLOCK_BODY)
    assert budget_at_snapshot["source"] == "history_estimate"
    assert budget_at_snapshot["timeout_seconds"] == 180

    def mutate_history_after_snapshot():
        # Runs when the transport launches the checker child, i.e. AFTER the
        # root snapshot was built and serialized and BEFORE readiness runs.
        _seed_history(store, _PYTEST_VC, duration_ms=200000, count=5)

    harness = _Harness(
        monkeypatch,
        tmp_path,
        _TWO_BLOCK_BODY,
        outcomes_by_call={2: TIMEOUT_OUTCOME},
        on_transport_launch=mutate_history_after_snapshot,
    )
    code, out = harness.produce(issue_number)
    assert code == 0 and out["status"] == "ok", out
    assert harness.transport_launches == 1

    # The store really changed: a fresh read now resolves a different budget.
    budget_after_mutation = _pytest_vc_budget_from_store(_TWO_BLOCK_BODY)
    assert budget_after_mutation["timeout_seconds"] != budget_at_snapshot["timeout_seconds"]
    assert budget_after_mutation["estimator_input_digest"] != budget_at_snapshot["estimator_input_digest"]

    # The persisted diagnostic keeps the snapshot-time budget, in BOTH
    # persisted artifacts, and equals what run_command() was really given.
    full_artifact, semantic_result, _raw_bytes = _persisted_artifacts(out)
    for diagnostics in (full_artifact["timeout_diagnostics"], semantic_result["timeout_diagnostics"]):
        (occurrence,) = diagnostics["occurrences"]
        assert occurrence["attribution"] == "attributed"
        provenance = occurrence["timeout_provenance"]
        assert provenance["source"] == "history_estimate"
        assert provenance["timeout_seconds"] == budget_at_snapshot["timeout_seconds"] == 180
        assert provenance["estimator_input_digest"] == budget_at_snapshot["estimator_input_digest"]
        assert provenance["timeout_seconds"] != budget_after_mutation["timeout_seconds"]
    timed_out_calls = [call for call in harness.run_command_calls if call[0] == _PYTEST_VC]
    assert {seconds for _command, seconds in timed_out_calls} == {180}


def _no_leak_run(monkeypatch, tmp_path) -> tuple[dict, dict]:
    stdout_marker = "STDOUT_SECRET_MARKER_5d1c"
    env_marker = "ENV_SECRET_MARKER_8e2f"
    leaky_timeout = (-1, stdout_marker, "timeout", 1234, {"RUNNER_ENV_SECRET": env_marker})
    harness = _Harness(monkeypatch, tmp_path, _TWO_BLOCK_BODY, outcomes_by_call={2: leaky_timeout})
    code, out = harness.produce(2897003)
    assert code == 0 and out["status"] == "ok", out
    return out, {"stdout": stdout_marker, "env": env_marker}


def test_no_leak_and_non_timeout_routes_unchanged(monkeypatch, tmp_path):
    # --- 1. no leak: exact key allowlist, no raw command / output / env -----
    out, markers = _no_leak_run(monkeypatch, tmp_path / "leak")
    _full, semantic_result, _bytes = _persisted_artifacts(out)
    diagnostics = semantic_result["timeout_diagnostics"]
    assert set(diagnostics) == _DIAGNOSTIC_TOP_LEVEL_KEYS
    for occurrence in diagnostics["occurrences"]:
        assert set(occurrence) == _DIAGNOSTIC_OCCURRENCE_KEYS
    serialized = json.dumps(diagnostics)
    for forbidden in (
        markers["stdout"],
        markers["env"],
        _PYTEST_VC,
        _PURE_VC,
        "runner_env_delta",
        "minimal_context",
        "raw_command",
        "stdout_head",
        "stderr_head",
    ):
        assert forbidden not in serialized, forbidden
    assert len(serialized.encode("utf-8")) <= 16 * 1024

    # --- 2. 17+ timeouts: bounded, no capture_failure ----------------------
    many_dir = tmp_path / "many"
    many_dir.mkdir()
    harness = _Harness(monkeypatch, many_dir, _TWENTY_TIMEOUTS_BODY, outcomes_by_call={0: TIMEOUT_OUTCOME})
    code, many_out = harness.produce(2897004)
    assert code == 0 and many_out["status"] == "ok", many_out
    many_attempt = _attempt_result(many_out, 2897004)
    assert many_attempt["transport_status"] == "ok"
    assert many_attempt["reason_code"] != "capture_failure"
    _full, many_semantic, _bytes = _persisted_artifacts(many_out)
    many_diagnostics = many_semantic["timeout_diagnostics"]
    assert many_diagnostics["total_timeout_occurrences"] == 20
    assert len(many_diagnostics["occurrences"]) == 16
    assert many_diagnostics["truncated_count"] == 4
    assert [o["occurrence_index"] for o in many_diagnostics["occurrences"]] == list(range(16))
    assert many_diagnostics["occurrences"][0]["execution_source"] == "executed"
    assert all(o["execution_source"] == "dedup_replay" for o in many_diagnostics["occurrences"][1:])
    assert len(json.dumps(many_diagnostics).encode("utf-8")) <= 16 * 1024
    assert many_out["canonical_step2_route"] == pipeline.STEP_5_OPERATOR_INTERVENTION_REQUIRED

    # --- 3. normal success: no diagnostic, no new gate ---------------------
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    harness = _Harness(monkeypatch, ok_dir, _APPROVE_BODY, default_outcome=SUCCESS_OUTCOME)
    code, ok_out = harness.produce(2897005)
    assert code == 0 and ok_out["status"] == "ok", ok_out
    assert ok_out["compact_result"]["verdict"] == "approve"
    assert ok_out["canonical_step2_route"] == pipeline.STEP_2_5
    assert "timeout_diagnostics" not in ok_out["merged_review_result"]
    _full, ok_semantic, _bytes = _persisted_artifacts(ok_out)
    assert "timeout_diagnostics" not in ok_semantic

    # --- 4. non-timeout human_judgment: no diagnostic, same route ----------
    hj_dir = tmp_path / "hj"
    hj_dir.mkdir()
    harness = _Harness(
        monkeypatch, hj_dir, _TWO_BLOCK_BODY, outcomes_by_call={2: (1, "", "boom", 9, {})}
    )
    code, hj_out = harness.produce(2897006)
    assert code == 0 and hj_out["status"] == "ok", hj_out
    assert hj_out["merged_review_result"]["failure_class"] == "contract_readiness_human_judgment"
    assert "timeout_diagnostics" not in hj_out["merged_review_result"]
    assert hj_out["canonical_step2_route"] == pipeline.STEP_5_OPERATOR_INTERVENTION_REQUIRED

    # --- 5. outer (aggregate wrapper) timeout: existing route, no diagnostic
    outer_dir = tmp_path / "outer"
    outer_dir.mkdir()
    harness = _Harness(
        monkeypatch, outer_dir, _TWO_BLOCK_BODY, baseline_supervisor_timed_out=True
    )
    code, outer_out = harness.produce(2897007)
    assert code == 2
    assert outer_out["status"] == "input_or_runtime_error"
    assert outer_out["error_code"] == "reviewer_transport_environment_failure"
    assert "merged_review_result" not in outer_out
    assert "timeout_diagnostics" not in json.dumps(outer_out)
    assert outer_out["canonical_step2_route"] == pipeline.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE

    # --- 6. compact V2 wire unchanged -------------------------------------
    wire = "\n".join(out["compact_result"]["stdout_lines"]).encode("utf-8") + b"\n"
    assert [line.split(": ", 1)[0] for line in out["compact_result"]["stdout_lines"]] == list(
        transport.V2_FIELDS
    )
    vta = out["verified_transport_artifact"]
    validated = transport.validate_compact_v2(
        wire, issue_number=2897003, invocation_id=vta["invocation_id"], attempt=vta["attempt"]
    )
    assert validated["validation_status"] == "valid", validated
    assert b"timeout_diagnostics" not in wire


def test_legacy_merged_result_without_diagnostics_is_still_a_valid_semantic_result():
    # `timeout_diagnostics` is an optional additive field: a legacy merged
    # result without it validates, and with it validates too.
    legacy = {
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "1",
        "verdict": "approve",
        "status": "ok",
        "body_sha256": "sha256:" + "0" * 64,
        "issue_kind": "implementation",
        "generated_at": "2026-10-04T00:00:00Z",
        "deterministic_checks": {},
        "blocking_issues": [],
        "structured_blockers": [],
        "non_blocking_improvements": [],
        "findings": [],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    }
    assert transport.validate_semantic_result_schema(legacy) is None
    with_diagnostics = dict(legacy, timeout_diagnostics={"schema_version": "TIMEOUT_DIAGNOSTICS_V1"})
    assert transport.validate_semantic_result_schema(with_diagnostics) is None


def _big_review_body() -> str:
    # 30 long pure VCs: the merged review result is large (~58 KB) even
    # without the diagnostic, so adding all 16 occurrences (~10 KB) overflows
    # the transport stdout cap although the diagnostic alone is < 16 KiB.
    blocks = "".join(
        f"```bash\n# AC{(index % 2) + 1}\n$ test -f d{index}/{'a' * 1450}.md\n```\n\n"
        for index in range(30)
    )
    return _BODY_HEADER + blocks + _ALLOWED


def test_large_review_result_with_timeouts_is_not_turned_into_capture_failure(
    monkeypatch, tmp_path
):
    # The budget the merge side enforces is the transport's own cap.
    assert cic.TIMEOUT_DIAGNOSTICS_STDOUT_CAP_BYTES == transport.STDOUT_CAP == 65_536

    def writer_bytes(result: dict) -> int:
        # `_cmd_run_checker_attempt()`: print(json.dumps(merged)) -> utf-8.
        return len((json.dumps(result) + "\n").encode("utf-8"))

    issue_number = 2897008
    harness = _Harness(
        monkeypatch,
        tmp_path,
        _big_review_body(),
        outcomes_by_call={call: TIMEOUT_OUTCOME for call in range(20)},
    )
    code, out = harness.produce(issue_number)
    assert code == 0 and out["status"] == "ok", out

    # Real transport: not a capture_failure, no retry storm, verified artifact.
    attempt = _attempt_result(out, issue_number)
    assert attempt["transport_status"] == "ok", attempt
    assert attempt["reason_code"] != "capture_failure"
    assert harness.transport_launches == 1
    full_artifact, semantic_result, _bytes = _persisted_artifacts(out)
    merged = out["merged_review_result"]
    assert full_artifact["verdict"] == semantic_result["verdict"] == "needs-fix"

    # Precondition of the scenario (computed with the real writer's options):
    # diagnostic-free result fits, the naive full diagnostic would not.
    base = {key: value for key, value in merged.items() if key != "timeout_diagnostics"}
    assert writer_bytes(base) <= transport.STDOUT_CAP
    assert len(harness.baseline_payloads[0]["results"]) == 30
    full_diagnostics = cic.build_timeout_diagnostics(
        harness.readiness_payloads[0], body_sha256=harness.body_sha256
    )
    assert len(full_diagnostics["occurrences"]) == 16
    assert writer_bytes(dict(base, timeout_diagnostics=full_diagnostics)) > transport.STDOUT_CAP

    # Routing-critical facts are intact; only the optional diagnostic shrank.
    assert merged["failure_class"] == "contract_readiness_human_judgment"
    assert out["canonical_step2_route"] == pipeline.STEP_5_OPERATOR_INTERVENTION_REQUIRED
    assert writer_bytes(merged) <= transport.STDOUT_CAP
    diagnostics = semantic_result["timeout_diagnostics"]
    assert diagnostics == merged["timeout_diagnostics"] == full_artifact["timeout_diagnostics"]
    assert diagnostics["total_timeout_occurrences"] == 20
    assert 0 < len(diagnostics["occurrences"]) < 16
    assert diagnostics["truncated_count"] == 20 - len(diagnostics["occurrences"])
    readback = harness.verified_readback(out, issue_number, "needs-fix")
    assert readback["verdict_identity"] is True, readback
