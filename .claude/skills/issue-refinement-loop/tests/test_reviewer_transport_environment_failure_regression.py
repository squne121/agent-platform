"""Issue #2165 AC3: transport-failure detection must survive the deadline fix.

Regression coverage that (a) genuine transport-layer faults (spawn failure,
signal, real hang) still collapse to `transport_status: environment_failure`
after the deadline/retry-policy changes, (b) a child-detected
`{"error_code": "timeout", "timeout_phase": ...}` (from a `contract_readiness_check.py`
`status: "runtime_error"` baseline_vc_preflight aggregate timeout, or from
`run_root_review_pipeline.py`'s own wrapper-level `subprocess.TimeoutExpired`)
is classified using the SAME `timeout` reason_code as this module's own
`process.wait()` timeout -- not a separate `inner_timeout` value (OWNER
2026-08-15 REQUEST_CHANGES P1-4) -- and (c) the deterministic backend's
retry policy is a CLOSED allowlist (P1-3): only `spawn_failure`/`signal`/
`capture_failure` retry; `timeout` and deterministic-output failures
(`nonzero_exit` etc.) do not, since retrying an identical deterministic
checker against an identical pinned Issue body cannot change the outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402

SHA = "sha256:" + "e" * 64


def _run(
    tmp_path: Path,
    program: str,
    *,
    backend: str = "fixture",
    per_attempt_deadline: int = 1,
    total_deadline: int = 5,
) -> dict:
    return transport.run_reviewer_transport(
        base_argv=[sys.executable, "-c", program],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend=backend,
        issue_number=2165,
        repo="example-org/example-repo",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="regression",
        session_ref="same-session",
        per_attempt_deadline=per_attempt_deadline,
        total_deadline=total_deadline,
    )


def test_given_spawn_failure_when_deadlines_are_production_defaults_then_still_environment_failure(tmp_path: Path):
    result = transport.run_reviewer_transport(
        base_argv=[str(tmp_path / "missing-reviewer")],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend="fixture",
        issue_number=2165,
        repo="example-org/example-repo",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="spawn-failure-regression",
        session_ref="same-session",
        # Deliberately use the module's production defaults here (no
        # per_attempt_deadline/total_deadline override) so this test fails
        # if a future edit widens them enough to change spawn-failure
        # behavior; spawn failure is immediate regardless of deadline size.
    )
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"spawn_failure"}
    assert result["attempts"][2]["session_ref"] is None


def test_given_real_hang_when_per_attempt_deadline_small_then_timeout_still_detected(tmp_path: Path):
    result = _run(tmp_path, "import time; time.sleep(5)", backend="fixture", per_attempt_deadline=1, total_deadline=2)
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"timeout"}
    assert result["attempts"][0]["timeout_phase"] == "reviewer_transport_wait"
    assert all(attempt["descendants_reaped"] is True for attempt in result["attempts"])


def test_given_signal_when_child_killed_then_still_environment_failure(tmp_path: Path):
    result = _run(tmp_path, "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert result["transport_status"] == "environment_failure"
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"signal"}


def test_given_child_detected_timeout_json_on_stderr_then_classified_timeout_not_nonzero_exit(tmp_path: Path):
    # Mirrors `run_root_review_pipeline.py`'s `_cmd_run_checker_attempt()`:
    # on an inner checker's `subprocess.TimeoutExpired` (or a
    # `contract_readiness_check.py` typed runtime_error), it prints
    # `{"error_code": "timeout", "timeout_phase": "..."}` to stderr and
    # exits 2.
    program = (
        "import json, sys\n"
        "print(json.dumps({'error_code': 'timeout', "
        "'timeout_phase': 'baseline_vc_preflight_aggregate'}), file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    result = _run(tmp_path, program, backend="deterministic")
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    # deterministic backend must NOT retry `timeout` -- exactly one
    # attempt, not the closed three-attempt matrix.
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["reason_code"] == "timeout"
    assert result["attempts"][0]["timeout_phase"] == "baseline_vc_preflight_aggregate"


def test_given_child_detected_timeout_json_without_phase_then_timeout_phase_is_unspecified(tmp_path: Path):
    program = "import json, sys\nprint(json.dumps({'error_code': 'timeout'}), file=sys.stderr)\nsys.exit(2)\n"
    result = _run(tmp_path, program, backend="deterministic")
    assert result["attempts"][0]["reason_code"] == "timeout"
    assert result["attempts"][0]["timeout_phase"] == "unspecified"


def test_given_unrelated_nonzero_exit_then_deterministic_backend_does_not_retry(tmp_path: Path):
    result = _run(tmp_path, "import sys; sys.exit(9)", backend="deterministic")
    assert result["transport_status"] == "environment_failure"
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"nonzero_exit"}
    # Issue #2165 P1-3: `nonzero_exit` is now excluded from the
    # deterministic backend's closed retryable allowlist -- retrying an
    # identical deterministic checker against an identical pinned body
    # cannot change the outcome, so exactly one attempt is made (this is a
    # BEHAVIOR CHANGE from the pre-#2165-fix-delta test, which asserted
    # `MAX_ATTEMPTS` retries here).
    assert len(result["attempts"]) == 1


def test_given_unrelated_nonzero_exit_then_other_backends_still_retry_unchanged(tmp_path: Path):
    result = _run(tmp_path, "import sys; sys.exit(9)", backend="fixture")
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"nonzero_exit"}
    assert len(result["attempts"]) == transport.MAX_ATTEMPTS


def test_given_nonjson_stderr_on_nonzero_exit_then_classification_falls_back_to_nonzero_exit(tmp_path: Path):
    program = "import sys; sys.stderr.write('not json at all'); sys.exit(2)"
    result = _run(tmp_path, program, backend="deterministic")
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"nonzero_exit"}


def test_given_stderr_json_without_timeout_error_code_then_classification_falls_back_to_nonzero_exit(tmp_path: Path):
    program = (
        "import json, sys\n"
        "print(json.dumps({'error_code': 'gh_auth_failed'}), file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    result = _run(tmp_path, program, backend="deterministic")
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"nonzero_exit"}


# ---------------------------------------------------------------------------
# P1-2: process-group reaping at the innermost VC-execution layer
# (`baseline_vc_preflight.py`'s `run_command()`) was ATTEMPTED but
# REVERTED -- a `Popen(start_new_session=True)` + `killpg()` rewrite
# requires replacing `run_command()`'s `subprocess.run(argv, ...)` call
# shape, which `.claude/skills/issue-contract-review/tests/
# test_pnpm_gate_security_boundary.py::test_producer_evidence_round_trips_to_triage`
# (an existing, pre-#2165 test OUTSIDE this Issue's Allowed Paths)
# monkeypatches directly and breaks on. See `run_command()`'s Issue #2165
# comment for detail and the PR description for the deferred follow-up.
#
# This module's OWN process-group reaping (`reviewer_transport.py`'s
# `_confirm_process_group_reaped()`), one layer further OUT, is unaffected
# and still covered by
# `test_given_real_hang_when_per_attempt_deadline_small_then_timeout_still_detected`
# above via `descendants_reaped`.
# ---------------------------------------------------------------------------
