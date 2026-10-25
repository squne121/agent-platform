"""
Tests for the isolation worktree agent Issue comment request bridge
(Issue #1633): materialize_isolation_issue_comment_request() ->
controlled_skill_mutation_exec.py --command-id issue_comment.publish.

No live GitHub network call happens in any test in this file: `gh` is never
actually invoked -- controlled_skill_mutation_exec.py's own `_find_gh_bin`,
`_verify_git_remote_origin`, `_post_gh_comment`, `_find_marker_matches`, and
`_readback_by_marker_literal` are all replaced with fakes before
`_exec.main()` is called.

AC4 coverage:
  positive: request -> materialize -> controlled executor -> fake gh POST ->
            marker readback success -> idempotent retry (no-op re-run)
  negative: absolute path / '..' / symlink / schema mismatch / wrong repo /
            wrong issue number / duplicate marker / marker-body conflict
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as _exec
from controlled_skill_mutation_policy import (
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    TRUSTED_REPO,
)

_PROJECT_ROOT = _GUARDS_DIR.parent.parent
_PUB_SCRIPTS_DIR = _PROJECT_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
if str(_PUB_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_PUB_SCRIPTS_DIR))

import isolation_issue_comment_bridge as _pub


def _build_request(issue_number, repo, comment_body, marker):
    """Build an ISOLATION_ISSUE_COMMENT_REQUEST_V1 via the production
    producer (Issue #1639 fix_delta P1-1: producer/consumer separation)."""
    return _pub.build_isolation_issue_comment_request(
        issue_number=issue_number,
        repo=repo,
        comment_body=comment_body,
        marker=marker,
    )


@pytest.fixture()
def tmp_project(tmp_path):
    """Fake project structure so controlled_skill_mutation_exec's own
    module-realpath / git-remote / project-root checks pass without touching
    the real repo."""
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    pub_dir = tmp_path / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    pub_dir.mkdir(parents=True)
    (pub_dir / "publish_termination_report.py").write_text("# stub\n")
    create_issue_dir = tmp_path / ".claude" / "skills" / "create-issue" / "scripts"
    create_issue_dir.mkdir(parents=True)
    (create_issue_dir / "prose_boundary_policy.py").write_text("# stub\n")

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)  # noqa: PLW1510
    subprocess.run(  # noqa: PLW1510
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            f"https://github.com/{TRUSTED_REPO}.git",
        ],
        capture_output=True,
    )
    return tmp_path


class TestPositivePublishFlow:
    """AC4 positive: bounded request -> materialize -> controlled executor ->
    fake gh POST -> marker readback success -> idempotent retry."""

    def test_full_bridge_success_path(self, tmp_project, monkeypatch):
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(
            1633,
            TRUSTED_REPO,
            "Loop terminated. <!-- CONTROLLED_EXEC_MARKER:abc123 -->",
            "<!-- CONTROLLED_EXEC_MARKER:abc123 -->",
        )
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""

        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_find_marker_matches", return_value=([], "")):
                    with patch.object(
                        _exec,
                        "_post_gh_comment",
                        return_value=("https://example.com/issues/1633#c1", "1", ""),
                    ):
                        with patch.object(
                            _exec,
                            "_readback_by_marker_literal",
                            return_value={
                                "comment_id": "1",
                                "comment_url": "https://example.com/issues/1633#c1",
                                "body_sha256": __import__("hashlib")
                                .sha256(
                                    "Loop terminated. <!-- CONTROLLED_EXEC_MARKER:abc123 -->".encode()  # noqa: UP012
                                )
                                .hexdigest(),
                            },
                        ):
                            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                                rc = _exec.main(
                                    [
                                        "--command-id",
                                        COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                        "--issue-number",
                                        "1633",
                                        "--input-file",
                                        rel_path,
                                        "--repo",
                                        TRUSTED_REPO,
                                        "--json",
                                    ]
                                )
        assert rc == 0

        marker_path = (
            tmp_project
            / "artifacts"
            / "1633"
            / "issue-metadata"
            / "issue_comment.publish"
            / "issue_comment_publish.marker.json"
        )
        assert marker_path.exists()

    def test_idempotent_retry_no_post(self, tmp_project, monkeypatch):
        """A second run against a remote that already has the marker must not
        call _post_gh_comment again (idempotent no-op)."""
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        comment_body = "Loop terminated. <!-- CONTROLLED_EXEC_MARKER:abc123 -->"
        request = _build_request(
            1633,
            TRUSTED_REPO,
            comment_body,
            "<!-- CONTROLLED_EXEC_MARKER:abc123 -->",
        )
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""

        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        remote_comment = {
            "id": "1",
            "url": "https://example.com/issues/1633#c1",
            "body": comment_body,
        }
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_find_marker_matches", return_value=([remote_comment], "")
                ):
                    with patch.object(_exec, "_post_gh_comment") as mock_post:
                        rc = _exec.main(
                            [
                                "--command-id",
                                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                "--issue-number",
                                "1633",
                                "--input-file",
                                rel_path,
                                "--repo",
                                TRUSTED_REPO,
                                "--json",
                            ]
                        )
        assert rc == 0
        mock_post.assert_not_called()


class TestNegativeFixtures:
    """AC4 negative: absolute path / '..' / symlink / hardlink / schema
    mismatch / wrong repo / wrong issue number / duplicate marker /
    marker-body conflict -- all denied with zero remote side effect."""

    def test_absolute_path_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(1633, TRUSTED_REPO, "hi <!-- m -->", "<!-- m -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""
        absolute = str((tmp_project / rel_path).resolve())
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment") as mock_post:
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number",
                            "1633",
                            "--input-file",
                            absolute,
                            "--repo",
                            TRUSTED_REPO,
                        ]
                    )
        assert rc == 2
        mock_post.assert_not_called()

    def test_dotdot_traversal_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(1633, TRUSTED_REPO, "hi <!-- m -->", "<!-- m -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""
        traversal = rel_path.replace(
            "issue-metadata/issue_comment.publish",
            "issue-metadata/../issue-metadata/issue_comment.publish",
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment") as mock_post:
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number",
                            "1633",
                            "--input-file",
                            traversal,
                            "--repo",
                            TRUSTED_REPO,
                        ]
                    )
        assert rc == 2
        mock_post.assert_not_called()

    def test_symlink_input_file_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(1633, TRUSTED_REPO, "hi <!-- m -->", "<!-- m -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""
        real_target = tmp_project / rel_path
        link_path = real_target.parent / "symlinked_input.json"
        link_path.symlink_to(real_target)
        # Use the symlink's own (unresolved) relative path -- resolving it
        # here would silently follow the symlink to its real target and
        # defeat the point of this negative fixture.
        rel_link = str(link_path.relative_to(tmp_project))
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment") as mock_post:
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number",
                            "1633",
                            "--input-file",
                            rel_link,
                            "--repo",
                            TRUSTED_REPO,
                        ]
                    )
        assert rc == 2
        mock_post.assert_not_called()

    def test_schema_mismatch_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        namespace_dir = (
            tmp_project / "artifacts" / "1633" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True)
        bad_input = namespace_dir / "bad.json"
        bad_input.write_text(
            json.dumps(
                {
                    "schema": "WRONG_SCHEMA_V1",
                    "issue_number": 1633,
                    "comment_body": "hi",
                    "marker": "hi",
                }
            )
        )
        rel_path = "artifacts/1633/issue-metadata/issue_comment.publish/bad.json"
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment") as mock_post:
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number",
                            "1633",
                            "--input-file",
                            rel_path,
                            "--repo",
                            TRUSTED_REPO,
                        ]
                    )
        assert rc == 2
        mock_post.assert_not_called()

    def test_wrong_repo_denied_at_executor(self, tmp_project, monkeypatch):
        """--repo not equal to TRUSTED_REPO is denied by the executor before
        any input-file/gh interaction, regardless of a validly materialized
        input file."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(1633, TRUSTED_REPO, "hi <!-- m -->", "<!-- m -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""
        with patch.object(_exec, "_post_gh_comment") as mock_post:
            rc = _exec.main(
                [
                    "--command-id",
                    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                    "--issue-number",
                    "1633",
                    "--input-file",
                    rel_path,
                    "--repo",
                    "attacker/evil-repo",
                ]
            )
        assert rc == 2
        mock_post.assert_not_called()

    def test_wrong_issue_number_denied_before_materialize(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(1633, TRUSTED_REPO, "hi <!-- m -->", "<!-- m -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment") as mock_post:
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number",
                            "9999",
                            "--input-file",
                            rel_path,
                            "--repo",
                            TRUSTED_REPO,
                        ]
                    )
        assert rc != 0
        mock_post.assert_not_called()

    def test_duplicate_marker_denied_before_post(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(1633, TRUSTED_REPO, "hi <!-- m -->", "<!-- m -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec,
                    "_find_marker_matches",
                    return_value=([{"id": "1", "body": "x"}, {"id": "2", "body": "y"}], ""),
                ):
                    with patch.object(_exec, "_post_gh_comment") as mock_post:
                        rc = _exec.main(
                            [
                                "--command-id",
                                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                "--issue-number",
                                "1633",
                                "--input-file",
                                rel_path,
                                "--repo",
                                TRUSTED_REPO,
                            ]
                        )
        assert rc == 1
        mock_post.assert_not_called()

    def test_marker_body_identity_conflict_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1633")
        request = _build_request(1633, TRUSTED_REPO, "hi <!-- m -->", "<!-- m -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert err == ""
        remote_comment = {"id": "42", "url": "https://ex/42", "body": "DIFFERENT BODY <!-- m -->"}
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_find_marker_matches", return_value=([remote_comment], "")
                ):
                    with patch.object(_exec, "_post_gh_comment") as mock_post:
                        rc = _exec.main(
                            [
                                "--command-id",
                                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                "--issue-number",
                                "1633",
                                "--input-file",
                                rel_path,
                                "--repo",
                                TRUSTED_REPO,
                            ]
                        )
        assert rc == 1
        mock_post.assert_not_called()

    def test_marker_not_embedded_denied_before_materialize(self, tmp_project):
        """The bounded request validator rejects a marker that is not a
        substring of comment_body before any file is materialized."""
        request = _build_request(1633, TRUSTED_REPO, "hi, no marker here", "<!-- missing -->")
        rel_path, err = _pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=1633,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_project,
        )
        assert rel_path is None
        assert "marker_not_embedded_in_body" in err
        assert not (tmp_project / "artifacts").exists()
