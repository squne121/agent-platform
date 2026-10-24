"""Regression coverage for the fresh_session_replacement resume bug (#2352).

PR #2142 owner REQUEST_CHANGES P0-1 introduced ``build_backend_command()`` as
the single place that materializes same-session/fresh-session resume
identity into real backend argv. ``run_reviewer_transport()``'s retry loop
had a latent bug where a fresh-session-replacement ``RetryIntent`` (whose
``session_ref`` is ``None`` by design -- see ``retry_matrix()``) was
incorrectly re-substituted with a synthetic UUID from
``generate_invocation_id()`` before being fed back into
``build_backend_command()`` as its next attempt's ``session_ref``. This
caused a non-existent synthetic session id to be passed to ``--resume`` for
the Claude backend even though there is no real session to resume.

This test spies on ``build_backend_command()`` directly (not just the
fixture-backend ``attempts[N]["session_ref"]`` field) so that it proves the
actual argv contract: a fresh-session-replacement attempt must be built
with ``session_ref=None`` and its resulting argv must not contain
``--resume`` at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402

SHA = "sha256:" + "e" * 64


def test_given_retry_matrix_when_fresh_session_replacement_then_next_attempt_builds_command_without_resume(
    tmp_path: Path, monkeypatch
):
    calls: list[dict] = []
    original_build_backend_command = transport.build_backend_command

    def spy_build_backend_command(*, backend, base_argv, session_ref):
        argv = original_build_backend_command(backend=backend, base_argv=base_argv, session_ref=session_ref)
        calls.append({"backend": backend, "session_ref": session_ref, "argv": argv})
        return argv

    monkeypatch.setattr(transport, "build_backend_command", spy_build_backend_command)

    result = transport.run_reviewer_transport(
        base_argv=[sys.executable, "-c", "import sys; sys.exit(9)"],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend="claude",
        issue_number=2352,
        repo="example-org/example-repo",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="fresh-session-resume-regression",
        session_ref="same-session",
        per_attempt_deadline=1,
        total_deadline=5,
    )

    # Closed three-attempt matrix (MAX_ATTEMPTS == 3):
    #   attempt 1: initial call with the caller-supplied session_ref
    #   attempt 2: same_session_resume retry (retry.session_ref == initial session_ref)
    #   attempt 3: fresh_session_replacement retry (retry.session_ref is None)
    assert len(result["attempts"]) == 3
    assert len(calls) == 3

    assert calls[0]["backend"] == "claude"
    assert calls[0]["session_ref"] == "same-session"
    assert "--resume" in calls[0]["argv"]

    assert calls[1]["session_ref"] == "same-session"
    assert "--resume" in calls[1]["argv"]

    # This is the decision-relevant assertion: the fresh-session-replacement
    # attempt's build_backend_command() call must receive session_ref=None
    # (not a synthetic generate_invocation_id() UUID), and the resulting
    # argv must therefore omit --resume entirely for the Claude backend.
    third_call = calls[2]
    assert third_call["session_ref"] is None
    assert "--resume" not in third_call["argv"]
    assert third_call["argv"] == [sys.executable, "-c", "import sys; sys.exit(9)"]
