"""V2 artifact security regression tests (Issue #2054)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402


def test_given_v2_artifact_when_verified_then_private_regular_file_is_accepted(tmp_path: Path):
    relative, digest = transport.write_semantic_artifact(
        artifact_root=tmp_path, issue_number=2054, repo="example-org/example-repo",
        invocation_id="security-parent", attempt=1, reviewed_body_sha256="sha256:" + "f" * 64,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    result = transport.verify_artifact(
        artifact_root=tmp_path, artifact_relative=relative, expected_repo="example-org/example-repo",
        expected_issue=2054, expected_body_sha256="sha256:" + "f" * 64,
        expected_invocation_id="security-parent", expected_attempt=1, expected_sha256=digest,
    )
    assert result["status"] == "valid"
    assert os.stat(tmp_path / relative).st_mode & 0o777 == 0o600


def test_given_v2_symlink_leaf_when_verified_then_no_follow_rejects_it(tmp_path: Path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    target = tmp_path / "target.json"
    target.write_text("{}")
    leaf = tmp_path / "2054" / "symlink-parent" / "attempt-001" / "compact_review_result_v2.json"
    leaf.parent.mkdir(parents=True)
    os.symlink(target, leaf)
    result = transport.verify_artifact(
        artifact_root=tmp_path, artifact_relative=str(leaf.relative_to(tmp_path)),
        expected_repo="example-org/example-repo", expected_issue=2054,
        expected_body_sha256="sha256:" + "0" * 64, expected_invocation_id="symlink-parent",
        expected_attempt=1, expected_sha256="sha256:" + "0" * 64,
    )
    assert result["status"] == "integrity_failure"
