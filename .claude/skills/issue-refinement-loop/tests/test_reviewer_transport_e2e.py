"""Real subprocess fault-injection coverage for the reviewer transport."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402

SHA = "sha256:" + "d" * 64


def _run(tmp_path: Path, program: str, *, session_ref: str | None = "same-session", total_deadline: int = 5) -> dict:
    return transport.run_reviewer_transport(
        base_argv=[sys.executable, "-c", program],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend="fixture",
        issue_number=2054,
        repo="example-org/example-repo",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="transport-e2e",
        session_ref=session_ref,
        per_attempt_deadline=1,
        total_deadline=total_deadline,
    )


def test_given_reviewer_json_when_parent_runs_then_parent_owns_v2_artifact(tmp_path: Path):
    result = _run(tmp_path, "import json; print(json.dumps({'verdict':'approve','blocking_issues':[]}))")
    assert result["transport_status"] == "ok"
    assert result["semantic_verdict"] == "approve"
    assert result["attempts"][0]["compact"]["SCHEMA"] == transport.SCHEMA_V2


def test_given_empty_stdout_when_parent_runs_then_environment_failure_not_human_judgment(tmp_path: Path):
    result = _run(tmp_path, "pass")
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"empty_output"}


def test_given_retry_matrix_when_retried_then_same_then_fresh_session_intent_is_bounded(tmp_path: Path):
    result = _run(tmp_path, "import sys; sys.exit(9)")
    assert len(result["attempts"]) == 3
    assert result["attempts"][0]["session_ref"] == "same-session"
    assert result["attempts"][1]["session_ref"] == "same-session"
    assert result["attempts"][2]["session_ref"] is None
    manifests = [
        json.loads(
            (tmp_path / "2054" / "transport-e2e" / f"attempt-{number:03d}" / "attempt_manifest.json").read_text()
        )
        for number in range(1, 4)
    ]
    assert [manifest["retry_intent"] for manifest in manifests] == [
        "initial",
        "same_session_resume",
        "fresh_session_replacement",
    ]


def test_given_spawn_failure_when_parent_cannot_start_child_then_every_attempt_has_immutable_receipt(tmp_path: Path):
    result = transport.run_reviewer_transport(
        base_argv=[str(tmp_path / "missing-reviewer")],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend="fixture",
        issue_number=2054,
        repo="example-org/example-repo",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="spawn-failure",
        session_ref="same-session",
        per_attempt_deadline=1,
        total_deadline=5,
    )
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert len(result["attempts"]) == 3
    for number, attempt in enumerate(result["attempts"], start=1):
        result_path = tmp_path / "2054" / "spawn-failure" / f"attempt-{number:03d}" / "attempt_result.json"
        assert json.loads(result_path.read_text())["reason_code"] == "spawn_failure"
        assert attempt["stdout_length"] == 0
        assert attempt["stderr_length"] == 0
        assert attempt["command_id"] == "issue-reviewer.run"


def test_given_parent_written_artifacts_when_checked_then_permissions_are_private(tmp_path: Path):
    result = _run(tmp_path, "import json; print(json.dumps({'verdict':'approve','blocking_issues':[]}))")
    assert result["transport_status"] == "ok"
    for artifact in tmp_path.rglob("*.json"):
        assert os.stat(artifact).st_mode & 0o777 == 0o600


def test_given_telemetry_when_recorded_then_no_raw_argv_or_output_is_stored(tmp_path: Path):
    result = _run(tmp_path, "print('not-json-secret-like-content')")
    attempt = result["attempts"][0]
    assert attempt["stdout_length"] > 0
    assert "not-json" not in attempt["stdout_prefix"]
    assert attempt["rendered_argv_sha256"].startswith("sha256:")


def test_given_stdout_flood_when_parent_captures_then_environment_failure_is_bounded(tmp_path: Path):
    result = _run(tmp_path, "print('x' * 70000)")
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"capture_failure"}
    assert all(attempt["stdout_length"] == 70001 for attempt in result["attempts"])
    assert all(attempt["stdout_prefix"] == "<redacted:65536-bytes>" for attempt in result["attempts"])


def test_given_signal_or_timeout_when_parent_reaps_process_group_then_failure_stays_nonsemantic(tmp_path: Path):
    signalled = _run(tmp_path / "signal", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert {attempt["reason_code"] for attempt in signalled["attempts"]} == {"signal"}
    timed_out = _run(tmp_path / "timeout", "import time; time.sleep(5)")
    assert {attempt["reason_code"] for attempt in timed_out["attempts"]} == {"timeout"}
    assert all(attempt["descendants_reaped"] is True for attempt in timed_out["attempts"])


def test_given_resume_fixture_when_backend_adapter_is_called_then_native_canary_is_not_claimed(tmp_path: Path):
    result = _run(tmp_path, "import sys; sys.exit(4)")
    assert result["attempts"][1]["session_ref"] == "same-session"
    assert result["attempts"][2]["session_ref"] != "same-session"


# ---------------------------------------------------------------------------
# PR #2142 owner REQUEST_CHANGES P0-1: backend command adapter materializes
# same-session/fresh-session resume identity into real argv.
# ---------------------------------------------------------------------------


def test_given_claude_backend_when_session_present_then_resume_flag_is_materialized():
    resumed = transport.build_backend_command(backend="claude", base_argv=["claude", "review"], session_ref="agent-123")
    assert resumed == ["claude", "review", "--resume", "agent-123"]
    fresh = transport.build_backend_command(backend="claude", base_argv=["claude", "review"], session_ref=None)
    assert fresh == ["claude", "review"]


def test_given_codex_backend_when_command_built_then_rejected_as_retired():
    # Issue #2161: the native Codex CLI executable backend ("codex") was
    # retired -- `codex` is no longer a member of `_KNOWN_BACKENDS`, so
    # `build_backend_command()` must reject it exactly like any other
    # unknown backend name, not build a `codex exec resume <id>` argv.
    assert "codex" not in transport._KNOWN_BACKENDS
    with pytest.raises(ValueError):
        transport.build_backend_command(backend="codex", base_argv=["codex", "exec"], session_ref="thread-9")


def test_given_unknown_backend_when_command_built_then_rejected():
    with pytest.raises(ValueError):
        transport.build_backend_command(backend="unknown-backend", base_argv=["x"], session_ref=None)


# ---------------------------------------------------------------------------
# PR #2142 owner REQUEST_CHANGES P0-2: wire <-> artifact semantic cross-binding.
# A self-consistent but tampered wire (ARTIFACT/ARTIFACT_SHA256 unchanged,
# VERDICT/BLOCKERS/NEXT_ACTION rewritten) must be detected.
# ---------------------------------------------------------------------------


def test_given_tampered_wire_when_cross_checked_against_artifact_then_mismatch_detected(tmp_path: Path):
    relative, digest = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=2054,
        repo="example-org/example-repo",
        invocation_id="tamper-check",
        attempt=1,
        reviewed_body_sha256=SHA,
        semantic_result={"verdict": "needs-fix", "blocking_issues": [{"code": "x"}]},
    )
    verified = transport.verify_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo="example-org/example-repo",
        expected_issue=2054,
        expected_body_sha256=SHA,
        expected_invocation_id="tamper-check",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert verified["status"] == "valid"

    # Self-consistent V2 grammar, ARTIFACT/ARTIFACT_SHA256 unchanged, but
    # VERDICT/BLOCKERS/NEXT_ACTION rewritten to disagree with the artifact's
    # actual semantic_result. `validate_compact_v2()` alone accepts this
    # (it only checks wire-internal consistency); the cross-check must not.
    tampered_wire = transport.build_compact_v2(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        reviewed_body_sha256=SHA,
        attempt_id="tamper-check",
        artifact_relative=relative,
        artifact_sha256=digest,
    )
    assert transport.validate_compact_v2(tampered_wire, issue_number=2054)["validation_status"] == "valid"

    cross = transport.verify_wire_matches_artifact(
        wire=tampered_wire, verified_artifact=verified, artifact_relative=relative, artifact_sha256=digest
    )
    assert cross["status"] == "integrity_failure"
    assert cross["reason_code"] == "wire_artifact_semantic_mismatch"

    genuine_wire = transport.project_compact_v2_from_artifact(
        verified["payload"], attempt_id="tamper-check", artifact_relative=relative, artifact_sha256=digest
    )
    genuine_cross = transport.verify_wire_matches_artifact(
        wire=genuine_wire, verified_artifact=verified, artifact_relative=relative, artifact_sha256=digest
    )
    assert genuine_cross["status"] == "valid"


def test_given_lossless_semantic_result_when_persisted_then_full_review_fields_survive(tmp_path: Path):
    full_review = {
        "verdict": "needs-fix",
        "blocking_issues": [{"code": "C1"}],
        "findings": [{"finding_kind": "checker_gap", "evidence": "x"}],
        "checker_evidence": [{"source_check": "check_issue_contract.py"}],
        "structured_blockers": [{"code": "C1", "blocking": True}],
        "body_sha256": SHA,
        "schema_version": "review_issue_result/v1",
    }
    relative, digest = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=2054,
        repo="example-org/example-repo",
        invocation_id="lossless-check",
        attempt=1,
        reviewed_body_sha256=SHA,
        semantic_result=full_review,
    )
    verified = transport.verify_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo="example-org/example-repo",
        expected_issue=2054,
        expected_body_sha256=SHA,
        expected_invocation_id="lossless-check",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert verified["status"] == "valid"
    assert verified["payload"]["semantic_result"] == full_review
    assert verified["payload"]["semantic_result"]["findings"] == full_review["findings"]
    assert verified["payload"]["semantic_result"]["checker_evidence"] == full_review["checker_evidence"]


# ---------------------------------------------------------------------------
# PR #2142 owner REQUEST_CHANGES P1-1: escaped grandchild must not hang the
# bounded reader thread join indefinitely.
# ---------------------------------------------------------------------------


def test_given_escaped_grandchild_holding_stdout_when_timeout_then_join_is_bounded(tmp_path: Path):
    program = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    time.sleep(60)\n"
        "    sys.exit(0)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = transport.run_reviewer_transport(
        base_argv=[sys.executable, "-c", program],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend="fixture",
        issue_number=2054,
        repo="example-org/example-repo",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="escaped-grandchild",
        session_ref="same-session",
        per_attempt_deadline=1,
        total_deadline=2,
    )
    elapsed = time.monotonic() - started
    assert result["transport_status"] == "environment_failure"
    # Bounded: must not block for anywhere near the grandchild's own 60s
    # sleep just because it still holds the inherited stdout/stderr pipe
    # open after the direct child (in the killed process group) has exited.
    assert elapsed < 30


# ---------------------------------------------------------------------------
# PR #2142 owner REQUEST_CHANGES P1-2: exact `os.open in os.supports_dir_fd`
# capability gate (not blanket `os.supports_dir_fd` truthiness).
# ---------------------------------------------------------------------------


def test_given_unsupported_dir_fd_capability_when_writing_then_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    with pytest.raises(OSError):
        transport.write_semantic_artifact(
            artifact_root=tmp_path,
            issue_number=2054,
            repo="example-org/example-repo",
            invocation_id="unsupported-cap-write",
            attempt=1,
            reviewed_body_sha256=SHA,
            semantic_result={"verdict": "approve", "blocking_issues": []},
        )


def test_given_unsupported_dir_fd_capability_when_reading_then_integrity_failure(tmp_path: Path, monkeypatch):
    relative, _digest = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=2054,
        repo="example-org/example-repo",
        invocation_id="unsupported-cap-read",
        attempt=1,
        reviewed_body_sha256=SHA,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    result = transport.secure_read_json(artifact_root=tmp_path, artifact_relative=relative)
    assert result["status"] == "integrity_failure"
    assert result["reason_code"] == "unsupported_secure_open_capability"


def test_given_dir_fd_set_missing_os_open_when_gated_then_rejected_even_if_nonempty(monkeypatch):
    """The set must be checked for `os.open` membership specifically, not
    merely truthiness (PR #2142 owner REQUEST_CHANGES P1-2)."""
    monkeypatch.setattr(os, "supports_dir_fd", frozenset({os.stat}))
    assert transport._has_secure_open_capability() is False


# ---------------------------------------------------------------------------
# PR #2142 owner REQUEST_CHANGES P1-3: producer-side write path must also
# reject a pre-existing symlink at an intermediate directory component.
# ---------------------------------------------------------------------------


def test_given_symlinked_intermediate_directory_when_producer_writes_then_no_follow_rejects(tmp_path: Path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    real_target = tmp_path / "real_target"
    real_target.mkdir()
    (tmp_path / "2054").symlink_to(real_target)
    with pytest.raises(OSError):
        transport.write_semantic_artifact(
            artifact_root=tmp_path,
            issue_number=2054,
            repo="example-org/example-repo",
            invocation_id="symlink-producer",
            attempt=1,
            reviewed_body_sha256=SHA,
            semantic_result={"verdict": "approve", "blocking_issues": []},
        )
    assert not any(real_target.rglob("*")), "no artifact must leak through the symlinked component"
