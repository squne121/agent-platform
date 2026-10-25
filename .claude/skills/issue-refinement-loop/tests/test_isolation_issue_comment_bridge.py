"""
test_isolation_issue_comment_bridge.py

Unit tests for isolation_issue_comment_bridge.py -- the parent-owned
producer/consumer bridge for the isolation worktree agent's bounded Issue
comment request (Issue #1633 / #1639).

Extracted from test_publish_termination_report.py (Issue #1873 Tranche 3):
the termination-report publisher these tests originally lived alongside was
removed, but this bridge is an independent, still-production
isolation-worktree `issue_comment.publish` lane.

AC coverage (retained from the original Issue #1633 / #1639 fix_delta
suite):
  - materialize_isolation_issue_comment_request(): namespace/schema
    correctness, field validation (marker embedding, empty fields, unknown
    fields, schema/repo/issue_number mismatch, non-dict request)
  - symlink/hardlink/TOCTOU safety of the underlying atomic write helper
  - post_isolation_issue_comment(): CONTROLLED_EXEC_MARKER injection /
    deterministic fallback marker derivation
"""

from __future__ import annotations

import json
import os
import stat as _stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import isolation_issue_comment_bridge as pub
from _trusted_repo import TRUSTED_REPO  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iicr(*, issue_number, repo, comment_body, marker):
    """Build an ISOLATION_ISSUE_COMMENT_REQUEST_V1 dict via the production
    producer (Issue #1639 fix_delta P1-1: producer/consumer separation)."""
    return pub.build_isolation_issue_comment_request(
        issue_number=issue_number, repo=repo, comment_body=comment_body, marker=marker,
    )


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
# Producer/consumer + symlink safety
# ---------------------------------------------------------------------------

class TestMaterializeIsolationIssueCommentRequest:
    """AC2 (Issue #1633): materialize_isolation_issue_comment_request() writes
    a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request into
    artifacts/{issue_number}/issue-metadata/issue_comment.publish/ as an
    ISSUE_COMMENT_PUBLISH_INPUT_V1 file, after validating the bounded fields.

    Issue #1639 fix_delta P1-1: the function now takes the already-built
    `request` object plus expected_issue_number/expected_repo, so these
    tests build the request via the production producer
    (build_isolation_issue_comment_request()) rather than letting
    materialize_isolation_issue_comment_request() reconstruct it from
    already-trusted keyword arguments."""

    def test_success_writes_expected_namespace_and_schema(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert err == ""
        assert rel_path == "artifacts/555/issue-metadata/issue_comment.publish/issue_comment_publish_input.json"
        written = json.loads((tmp_path / rel_path).read_text(encoding="utf-8"))
        assert written["schema"] == "ISSUE_COMMENT_PUBLISH_INPUT_V1"
        assert written["issue_number"] == 555
        assert written["comment_body"] == "hello <!-- m1 -->"
        assert written["marker"] == "<!-- m1 -->"

    def test_rejects_marker_not_embedded_in_body(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="hello world",
            marker="<!-- missing -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "marker_not_embedded_in_body" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_empty_marker(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="hello world",
            marker="",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "marker" in err

    def test_rejects_empty_comment_body(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="",
            marker="",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path is None
        assert err != ""

    def test_rejects_unknown_field_in_request(self, tmp_path):
        """Issue #1639 fix_delta P1-1: unknown-field requests must be
        rejected through the real production entry point (not just the
        validator function in isolation)."""
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        request["extra_field"] = "unexpected"
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "unknown_fields" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_schema_mismatch(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        request["schema"] = "WRONG_SCHEMA_V1"
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "schema_mismatch" in err

    def test_rejects_repo_mismatch(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="someone-else/other-repo",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "repo_mismatch" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_issue_number_mismatch(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo=TRUSTED_REPO,
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=999,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "issue_number_mismatch" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_non_dict_request(self, tmp_path):
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=["not", "a", "dict"],
            expected_issue_number=555,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path is None
        assert err != ""


# ---------------------------------------------------------------------------
# Issue #1639 fix_delta P0: materializer symlink/hardlink/TOCTOU safety.
#
# These tests exercise _write_json_atomic_symlink_safe() /
# materialize_isolation_issue_comment_request() against adversarial
# pre-planted filesystem state at every level of the write path (namespace
# directory ancestors, the namespace directory itself, and the final output
# filename), and assert fail-closed behavior that never touches (or only
# ever atomically replaces, never truncates-through) a pre-existing dirent.
# ---------------------------------------------------------------------------

class TestMaterializerSymlinkSafety:
    def _request(self, issue_number=777, body="hello <!-- m1 -->", marker="<!-- m1 -->"):
        return _iicr(
            issue_number=issue_number,
            repo=TRUSTED_REPO,
            comment_body=body,
            marker=marker,
        )

    def test_rejects_preexisting_output_symlink_without_touching_target(self, tmp_path):
        """The predictable output path is pre-planted as a symlink pointing
        at a sentinel target file. Materialization must fail closed and the
        sentinel target's content must remain completely unchanged."""
        target = tmp_path / "sentinel_target.json"
        sentinel_content = '{"do_not_touch": "sentinel-value-12345"}'
        target.write_text(sentinel_content, encoding="utf-8")

        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_path = namespace_dir / "issue_comment_publish_input.json"
        out_path.symlink_to(target)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        # The symlink itself must still point at target, untouched.
        assert out_path.is_symlink()
        # Target content must be byte-identical to what it was before.
        assert target.read_text(encoding="utf-8") == sentinel_content

    def test_rejects_namespace_directory_itself_as_symlink(self, tmp_path):
        """artifacts/{issue}/issue-metadata/issue_comment.publish is itself
        pre-planted as a symlink to an unrelated directory."""
        real_other_dir = tmp_path / "unrelated_dir"
        real_other_dir.mkdir()
        sentinel = real_other_dir / "sentinel.txt"
        sentinel.write_text("do-not-touch", encoding="utf-8")

        parent = tmp_path / "artifacts" / "777" / "issue-metadata"
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "issue_comment.publish").symlink_to(real_other_dir)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert not (real_other_dir / "issue_comment_publish_input.json").exists()
        assert sentinel.read_text(encoding="utf-8") == "do-not-touch"

    def test_rejects_intermediate_directory_as_symlink(self, tmp_path):
        """An intermediate ancestor (artifacts/{issue}/issue-metadata) is
        pre-planted as a symlink, not a real directory."""
        real_other_dir = tmp_path / "unrelated_parent"
        real_other_dir.mkdir()

        issue_dir = tmp_path / "artifacts" / "777"
        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / "issue-metadata").symlink_to(real_other_dir)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert list(real_other_dir.iterdir()) == []

    def test_rejects_preexisting_output_as_hardlink(self, tmp_path):
        """The predictable output path already exists as a hardlink to
        another file (st_nlink > 1); materialization must fail closed and
        must not modify the shared inode's content."""
        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_path = namespace_dir / "issue_comment_publish_input.json"
        original_content = '{"do_not_touch": "hardlink-sentinel"}'
        out_path.write_text(original_content, encoding="utf-8")

        other_link = tmp_path / "other_hardlink.json"
        os.link(out_path, other_link)
        assert out_path.stat().st_nlink == 2

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert out_path.read_text(encoding="utf-8") == original_content
        assert other_link.read_text(encoding="utf-8") == original_content

    def test_rejects_preexisting_output_as_directory(self, tmp_path):
        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_dir = namespace_dir / "issue_comment_publish_input.json"
        out_dir.mkdir()
        (out_dir / "keep.txt").write_text("do-not-touch", encoding="utf-8")

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert out_dir.is_dir()
        assert (out_dir / "keep.txt").read_text(encoding="utf-8") == "do-not-touch"

    def test_rejects_preexisting_output_as_fifo(self, tmp_path):
        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_path = namespace_dir / "issue_comment_publish_input.json"
        os.mkfifo(out_path)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert _stat.S_ISFIFO(out_path.lstat().st_mode)

    def test_concurrent_replacement_leaves_atomic_result(self, tmp_path):
        """Simulate a concurrent writer racing the same output path: run
        materialize twice back-to-back with different bodies and assert the
        final file is one complete, self-consistent JSON document (never a
        half-written / interleaved result) -- exercising the atomic
        temp-file + rename() replace path for legitimate re-materialization."""
        rel_path_1, err_1 = pub.materialize_isolation_issue_comment_request(
            request=self._request(body="first <!-- m1 -->", marker="<!-- m1 -->"),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert err_1 == ""

        rel_path_2, err_2 = pub.materialize_isolation_issue_comment_request(
            request=self._request(body="second <!-- m2 -->", marker="<!-- m2 -->"),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert err_2 == ""
        assert rel_path_1 == rel_path_2

        written = json.loads((tmp_path / rel_path_2).read_text(encoding="utf-8"))
        assert written["comment_body"] == "second <!-- m2 -->"
        assert written["marker"] == "<!-- m2 -->"

        # No stray temp files should remain in the namespace directory.
        namespace_dir = (tmp_path / rel_path_2).parent
        leftover_tmp = [p for p in namespace_dir.iterdir() if ".tmp." in p.name]
        assert leftover_tmp == []

    def test_materialization_failure_does_not_destroy_existing_legit_file(self, tmp_path):
        """A legitimate prior materialization exists at the output path.
        A subsequent materialize call that fails validation (e.g. marker
        not embedded) must not touch the existing legitimate file at all."""
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(body="original <!-- m1 -->", marker="<!-- m1 -->"),
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert err == ""
        original_bytes = (tmp_path / rel_path).read_bytes()

        bad_request = _iicr(
            issue_number=777,
            repo=TRUSTED_REPO,
            comment_body="hello world",
            marker="<!-- not-embedded -->",
        )
        rel_path_2, err_2 = pub.materialize_isolation_issue_comment_request(
            request=bad_request,
            expected_issue_number=777,
            expected_repo=TRUSTED_REPO,
            project_root=tmp_path,
        )
        assert rel_path_2 is None
        assert err_2 != ""

        assert (tmp_path / rel_path).read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# P0-5 / Issue #1633: CONTROLLED_EXEC_MARKER env var (or a deterministic
# content-hash fallback) is embedded into the materialized comment_body as
# the bounded request's marker field, via post_isolation_issue_comment().
# ---------------------------------------------------------------------------

class TestExecMarkerInjection:
    def test_post_isolation_issue_comment_injects_env_marker(self, tmp_path, monkeypatch):
        """post_isolation_issue_comment materializes the marker into
        comment_body when exec_marker is provided (or CONTROLLED_EXEC_MARKER
        env var is set)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            rc = pub.post_isolation_issue_comment(
                issue_number=42,
                body="## Test Body",
                repo=TRUSTED_REPO,
                exec_marker="testmarker99",
            )

        assert rc == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "<!-- CONTROLLED_EXEC_MARKER:testmarker99 -->" in materialized["comment_body"]
        assert "## Test Body" in materialized["comment_body"]

    def test_no_exec_marker_uses_deterministic_fallback(self, tmp_path, monkeypatch):
        """When exec_marker is not provided and CONTROLLED_EXEC_MARKER is
        not set, a deterministic content-hash marker is used instead
        (materializer still requires a non-empty marker embedded in
        comment_body -- Issue #1633 AC1)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        monkeypatch.delenv("CONTROLLED_EXEC_MARKER", raising=False)

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            rc = pub.post_isolation_issue_comment(
                issue_number=42,
                body="## Test Body",
                repo=TRUSTED_REPO,
            )

        assert rc == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "testmarker99" not in materialized["comment_body"]
        assert materialized["marker"] in materialized["comment_body"]

    def test_fallback_marker_differs_across_issue_numbers_for_same_body(self):
        """Issue #1639 fix_delta P1-2: fallback marker must not collide when
        the exact same body is posted to a different issue -- it must hash
        repo + issue_number + body, not body alone."""
        same_body = "## Identical Report Body"

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        import tempfile
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1, root2 = Path(td1), Path(td2)
            with patch.object(pub, "_PROJECT_ROOT", root1):
                with patch("subprocess.run", side_effect=fake_run):
                    pub.post_isolation_issue_comment(issue_number=1, body=same_body, repo=TRUSTED_REPO)
            with patch.object(pub, "_PROJECT_ROOT", root2):
                with patch("subprocess.run", side_effect=fake_run):
                    pub.post_isolation_issue_comment(issue_number=2, body=same_body, repo=TRUSTED_REPO)

            marker_for_issue_1 = _read_materialized_issue_comment_input(root1, 1)["marker"]
            marker_for_issue_2 = _read_materialized_issue_comment_input(root2, 2)["marker"]
            assert marker_for_issue_1 != marker_for_issue_2

    def test_fallback_marker_differs_across_repos_for_same_body_and_issue(self):
        """Same body + same issue_number but different repo must also
        produce a different fallback marker."""
        import tempfile

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        same_body = "## Identical Report Body"
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1, root2 = Path(td1), Path(td2)
            with patch.object(pub, "_PROJECT_ROOT", root1):
                with patch("subprocess.run", side_effect=fake_run):
                    pub.post_isolation_issue_comment(issue_number=1, body=same_body, repo=TRUSTED_REPO)
            with patch.object(pub, "_PROJECT_ROOT", root2):
                with patch("subprocess.run", side_effect=fake_run):
                    pub.post_isolation_issue_comment(issue_number=1, body=same_body, repo="someone-else/other-repo")

            marker_repo_1 = _read_materialized_issue_comment_input(root1, 1)["marker"]
            marker_repo_2 = _read_materialized_issue_comment_input(root2, 1)["marker"]
            assert marker_repo_1 != marker_repo_2

    def test_marker_appended_after_body_content(self, tmp_path, monkeypatch):
        """Marker is appended after original body, not prepended."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        original_body = "## Original Content\n\nSome text."

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            pub.post_isolation_issue_comment(
                issue_number=42,
                body=original_body,
                repo=TRUSTED_REPO,
                exec_marker="markerXYZ",
            )

        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        body = materialized["comment_body"]
        # Marker comes after original content
        marker_pos = body.find("<!-- CONTROLLED_EXEC_MARKER:")
        original_end_pos = body.find(original_body) + len(original_body)
        assert marker_pos > original_end_pos - 1  # marker is after original body
