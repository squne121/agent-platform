"""
test_publish_termination_report.py

Integration tests for publish_termination_report.py (Issue #692).

#1873 (bounded review loops): this module no longer invokes
render_termination_report.py as a subprocess -- the orchestrator now
assembles a plain markdown termination summary directly and passes it to
`publish()`/`main()` as `body` (stdin or --body-file). The renderer-specific
tests that used to live here (subprocess shell=False on the renderer,
TERMINATION_REPORT_RENDER_RESULT_V1 validation, loop_handoff forwarding to
the renderer) were removed along with render_termination_report.py. What
remains is the `_post_github_comment()` / issue_comment.publish controlled
mutation lane mechanics, which are unchanged.

AC coverage:
  AC3: publishable body -> issue_comment.publish controlled executor invoked
       with --input-file (Issue #1633; raw `gh issue comment` is not called
       directly)
  AC4: fail-closed cases -> gh NOT called
  AC5: failure cases -> artifact recorded (reason_code)
  AC8: fake gh integration tests: normal post / empty-body no-post / gh
       failure fail-closed
  P0-5: CONTROLLED_EXEC_MARKER injection (or deterministic fallback) into
       the materialized comment_body
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import publish_termination_report as pub
from _trusted_repo import TRUSTED_REPO  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_exec_proc(returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a fake CompletedProcess-like object for the
    controlled_skill_mutation_exec.py subprocess.run mock (Issue #1633)."""
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    m.stdout = ""
    return m


def _is_exec_call(cmd) -> bool:
    """True iff cmd is a subprocess.run invocation of
    controlled_skill_mutation_exec.py (Issue #1633 issue_comment.publish
    bridge)."""
    return (
        isinstance(cmd, list)
        and len(cmd) > 1
        and str(cmd[1]).endswith("controlled_skill_mutation_exec.py")
    )


def _read_materialized_issue_comment_input(project_root: Path, issue_number: int) -> dict:
    """Read back the ISSUE_COMMENT_PUBLISH_INPUT_V1 JSON that
    materialize_isolation_issue_comment_request() wrote for issue_number."""
    path = (
        project_root / "artifacts" / str(issue_number)
        / "issue-metadata" / "issue_comment.publish" / "issue_comment_publish_input.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC3: publishable body -> issue_comment.publish controlled executor
# ---------------------------------------------------------------------------

class TestGhBodyFile:
    """AC3 (Issue #1633): _post_github_comment materializes a bounded
    ISOLATION_ISSUE_COMMENT_REQUEST_V1 request and launches
    controlled_skill_mutation_exec.py --command-id issue_comment.publish
    with --input-file; raw `gh issue comment --body-file` is never called
    directly from this module."""

    def test_gh_called_with_body_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        exec_calls: list[list] = []

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            exec_calls.append(cmd)
            return _fake_exec_proc()

        with patch("subprocess.run", side_effect=fake_run):
            result = pub.publish(issue_number=42, body="## Report", repo=TRUSTED_REPO)

        assert result == 0
        assert len(exec_calls) == 1
        exec_cmd = exec_calls[0]
        assert exec_cmd[1].endswith("controlled_skill_mutation_exec.py")
        assert "--command-id" in exec_cmd
        assert exec_cmd[exec_cmd.index("--command-id") + 1] == "issue_comment.publish"
        assert "--input-file" in exec_cmd
        input_file_value = exec_cmd[exec_cmd.index("--input-file") + 1]
        # Must be a project-root-relative path (the executor rejects absolute paths)
        assert not input_file_value.startswith("/")
        assert input_file_value.startswith("artifacts/42/issue-metadata/issue_comment.publish/")

    def test_gh_body_file_receives_correct_content(self, tmp_path, monkeypatch):
        """Body is materialized into the ISSUE_COMMENT_PUBLISH_INPUT_V1 JSON
        file, not passed via stdin to a raw gh call."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        expected_body = "## Refinement Loop: Approved\n\nApproved."

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc()

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, body=expected_body, repo=TRUSTED_REPO)

        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert materialized["comment_body"].startswith(expected_body)
        assert materialized["schema"] == "ISSUE_COMMENT_PUBLISH_INPUT_V1"
        assert materialized["issue_number"] == 42

    def test_gh_has_prompt_disabled_env(self, tmp_path, monkeypatch):
        """The controlled_skill_mutation_exec.py invocation must have
        GH_PROMPT_DISABLED=1 in env (inherited down to its own gh call)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        exec_envs: list[dict] = []

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            exec_envs.append(kwargs.get("env", {}))
            return _fake_exec_proc()

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, body="## Report", repo=TRUSTED_REPO)

        assert len(exec_envs) == 1
        assert exec_envs[0].get("GH_PROMPT_DISABLED") == "1"

    def test_gh_timeout_fail_closed(self, tmp_path, monkeypatch):
        """controlled_skill_mutation_exec.py timeout (30s) must fail closed
        (return -1, record artifact)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        import subprocess as _subprocess

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            raise _subprocess.TimeoutExpired(cmd, 30)

        artifact_calls: list[dict] = []

        def capture_artifact(**kwargs):
            artifact_calls.append(kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(pub, "_record_artifact", side_effect=capture_artifact):
                exit_code = pub.publish(issue_number=42, body="## Report", repo=TRUSTED_REPO)

        assert exit_code == 1
        assert len(artifact_calls) == 1
        assert artifact_calls[0]["reason_code"] == "gh_comment_timeout"

    def test_gh_failure_records_artifact_and_fails_closed(self, tmp_path, monkeypatch):
        """A nonzero exit from controlled_skill_mutation_exec.py must fail
        closed (return 1, record artifact)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc(returncode=1, stderr="boom")

        artifact_calls: list[dict] = []

        def capture_artifact(**kwargs):
            artifact_calls.append(kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(pub, "_record_artifact", side_effect=capture_artifact):
                exit_code = pub.publish(issue_number=42, body="## Report", repo=TRUSTED_REPO)

        assert exit_code == 1
        assert artifact_calls[0]["reason_code"] == "gh_comment_failed"


# ---------------------------------------------------------------------------
# AC4/AC5: empty body -> fail closed without calling gh
# ---------------------------------------------------------------------------

class TestEmptyBodyFailClosed:
    def test_empty_body_is_fail_closed_no_gh_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            raise AssertionError(f"gh must not be called for an empty body: {cmd}")

        artifact_calls: list[dict] = []

        def capture_artifact(**kwargs):
            artifact_calls.append(kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(pub, "_record_artifact", side_effect=capture_artifact):
                exit_code = pub.publish(issue_number=42, body="   ", repo=TRUSTED_REPO)

        assert exit_code == 1
        assert artifact_calls[0]["reason_code"] == "empty_body"

    def test_publishable_body_normal_post(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc()

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, body="## Approved", repo=TRUSTED_REPO)

        assert exit_code == 0


# ---------------------------------------------------------------------------
# AC10 (Issue #1166) / Issue #1633: --repo passthrough
# ---------------------------------------------------------------------------

class TestRepoFlag:
    """--repo is passed through unchanged to the issue_comment.publish
    controlled executor invocation."""

    def test_gh_command_includes_repo_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        exec_calls: list[list] = []

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            exec_calls.append(cmd)
            return _fake_exec_proc()

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, body="## Report", repo=TRUSTED_REPO)

        assert len(exec_calls) == 1
        exec_cmd = exec_calls[0]
        assert "--repo" in exec_cmd, "--repo flag must be present on the executor invocation"
        repo_idx = exec_cmd.index("--repo")
        assert repo_idx + 1 < len(exec_cmd), "--repo must be followed by repo value"
        assert exec_cmd[repo_idx + 1] == TRUSTED_REPO

    def test_post_github_comment_passes_repo_to_gh(self, tmp_path, monkeypatch):
        """_post_github_comment must include --repo on the executor invocation."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        exec_calls: list[list] = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_calls.append(cmd)
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            rc = pub._post_github_comment(
                issue_number=42,
                body="## Test",
                repo=TRUSTED_REPO,
            )

        assert rc == 0
        assert len(exec_calls) == 1
        assert "--repo" in exec_calls[0]
        idx = exec_calls[0].index("--repo")
        assert exec_calls[0][idx + 1] == TRUSTED_REPO


class TestTerminationReportDocsProse:
    def test_english_duplicate_prose_removed(self):
        root = Path(__file__).resolve().parent.parent
        text = "\n".join([
            (root / "SKILL.md").read_text(),
            (root / "references" / "termination-policy.md").read_text(),
        ])

        forbidden = [
            "human_escalation example includes termination_cause and blockers_summary",
            "legacy alias blocker_summary is normalized to canonical blockers_summary",
            "owner decision is required",
            "conflicting scope signals remain unresolved",
        ]

        for phrase in forbidden:
            assert phrase not in text, f"英語重複 prose が残っています: {phrase!r}"


# ---------------------------------------------------------------------------
# P0-5: Exec marker injection in _post_github_comment
# ---------------------------------------------------------------------------

class TestExecMarkerInjection:
    """P0-5 / Issue #1633: CONTROLLED_EXEC_MARKER env var (or a deterministic
    content-hash fallback) is embedded into the materialized comment_body as
    the bounded request's marker field."""

    def test_marker_injected_into_body_when_env_set(self, tmp_path, monkeypatch):
        """When CONTROLLED_EXEC_MARKER is set, comment body includes marker."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc()

        monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "abc123marker456")

        with patch("subprocess.run", side_effect=fake_run):
            result = pub.publish(issue_number=42, body="## Report", repo=TRUSTED_REPO)

        assert result == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "<!-- CONTROLLED_EXEC_MARKER:abc123marker456 -->" in materialized["comment_body"]
        assert materialized["marker"] == "<!-- CONTROLLED_EXEC_MARKER:abc123marker456 -->"

    def test_no_marker_injected_when_env_not_set(self, tmp_path, monkeypatch):
        """When CONTROLLED_EXEC_MARKER is not set, a deterministic
        content-hash marker is used instead (materializer still requires a
        non-empty marker embedded in comment_body -- Issue #1633 AC1)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc()

        monkeypatch.delenv("CONTROLLED_EXEC_MARKER", raising=False)

        with patch("subprocess.run", side_effect=fake_run):
            result = pub.publish(issue_number=42, body="## Report", repo=TRUSTED_REPO)

        assert result == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "abc123marker456" not in materialized["comment_body"]
        assert materialized["marker"] in materialized["comment_body"]

    def test_fallback_marker_differs_across_issue_numbers_for_same_body(self, monkeypatch):
        """Issue #1639 fix_delta P1-2: fallback marker must not collide when
        the exact same body is posted to a different issue -- it must hash
        repo + issue_number + body, not body alone."""
        monkeypatch.delenv("CONTROLLED_EXEC_MARKER", raising=False)
        same_body = "## Identical Report Body"

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc()

        import tempfile
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1, root2 = Path(td1), Path(td2)
            with patch.object(pub, "_PROJECT_ROOT", root1):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=1, body=same_body, repo=TRUSTED_REPO)
            with patch.object(pub, "_PROJECT_ROOT", root2):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=2, body=same_body, repo=TRUSTED_REPO)

            marker_for_issue_1 = _read_materialized_issue_comment_input(root1, 1)["marker"]
            marker_for_issue_2 = _read_materialized_issue_comment_input(root2, 2)["marker"]
            assert marker_for_issue_1 != marker_for_issue_2

    def test_fallback_marker_differs_across_repos_for_same_body_and_issue(self):
        """Same body + same issue_number but different repo must also
        produce a different fallback marker."""
        import tempfile

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc()

        same_body = "## Identical Report Body"
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1, root2 = Path(td1), Path(td2)
            with patch.object(pub, "_PROJECT_ROOT", root1):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=1, body=same_body, repo=TRUSTED_REPO)
            with patch.object(pub, "_PROJECT_ROOT", root2):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=1, body=same_body, repo="someone-else/other-repo")

            marker_repo_1 = _read_materialized_issue_comment_input(root1, 1)["marker"]
            marker_repo_2 = _read_materialized_issue_comment_input(root2, 1)["marker"]
            assert marker_repo_1 != marker_repo_2

    def test_post_github_comment_injects_marker(self, tmp_path, monkeypatch):
        """_post_github_comment materializes the marker into comment_body when
        CONTROLLED_EXEC_MARKER is set."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "testmarker99")

        with patch("subprocess.run", side_effect=fake_run):
            rc = pub._post_github_comment(
                issue_number=42,
                body="## Test Body",
                repo=TRUSTED_REPO,
            )

        assert rc == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "<!-- CONTROLLED_EXEC_MARKER:testmarker99 -->" in materialized["comment_body"]
        assert "## Test Body" in materialized["comment_body"]

    def test_marker_appended_after_body_content(self, tmp_path, monkeypatch):
        """Marker is appended after original body, not prepended."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        original_body = "## Original Content\n\nSome text."

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "markerXYZ")

        with patch("subprocess.run", side_effect=fake_run):
            pub._post_github_comment(
                issue_number=42,
                body=original_body,
                repo=TRUSTED_REPO,
            )

        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        body = materialized["comment_body"]
        # Marker comes after original content
        marker_pos = body.find("<!-- CONTROLLED_EXEC_MARKER:")
        original_end_pos = body.find(original_body) + len(original_body)
        assert marker_pos > original_end_pos - 1  # marker is after original body


# ---------------------------------------------------------------------------
# CLI entry point (--body-file / stdin)
# ---------------------------------------------------------------------------

class TestCliEntryPoint:
    def test_main_reads_body_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        body_file = tmp_path / "body.md"
        body_file.write_text("## Approved\n\nDone.", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            assert _is_exec_call(cmd)
            return _fake_exec_proc()

        argv = [
            "publish_termination_report.py",
            "--issue-number", "42",
            "--repo", TRUSTED_REPO,
            "--body-file", str(body_file),
        ]
        monkeypatch.setattr(sys, "argv", argv)

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.main()

        assert exit_code == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert materialized["comment_body"].startswith("## Approved")

    def test_main_rejects_empty_body(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        body_file = tmp_path / "body.md"
        body_file.write_text("   \n", encoding="utf-8")

        argv = [
            "publish_termination_report.py",
            "--issue-number", "42",
            "--repo", TRUSTED_REPO,
            "--body-file", str(body_file),
        ]
        monkeypatch.setattr(sys, "argv", argv)

        exit_code = pub.main()
        assert exit_code == 2
