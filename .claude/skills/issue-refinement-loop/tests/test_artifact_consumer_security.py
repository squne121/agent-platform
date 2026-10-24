"""Directory-FD artifact consumer regression tests (Issue #2054)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402


def _artifact(root: Path) -> tuple[str, str]:
    relative, digest = transport.write_semantic_artifact(
        artifact_root=root,
        issue_number=2054,
        repo="example-org/example-repo",
        invocation_id="secure-invocation",
        attempt=1,
        reviewed_body_sha256="sha256:" + "c" * 64,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    return relative, digest


def test_given_bound_regular_file_when_verified_then_same_raw_bytes_are_accepted(tmp_path: Path):
    relative, digest = _artifact(tmp_path)
    result = transport.verify_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo="example-org/example-repo",
        expected_issue=2054,
        expected_body_sha256="sha256:" + "c" * 64,
        expected_invocation_id="secure-invocation",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert result["status"] == "valid"


def test_given_symlink_component_when_verified_then_integrity_failure(tmp_path: Path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    relative, digest = _artifact(tmp_path)
    # A sibling root with a symlinked first component must never be followed.
    evil = tmp_path / "evil"
    os.symlink(tmp_path / "2054", evil)
    result = transport.verify_artifact(
        artifact_root=tmp_path,
        artifact_relative="evil/secure-invocation/attempt-001/compact_review_result_v2.json",
        expected_repo="example-org/example-repo",
        expected_issue=2054,
        expected_body_sha256="sha256:" + "c" * 64,
        expected_invocation_id="secure-invocation",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert result["status"] == "integrity_failure"


def test_given_existing_attempt_when_written_then_immutable_rejects_overwrite(tmp_path: Path):
    _artifact(tmp_path)
    with pytest.raises(FileExistsError):
        _artifact(tmp_path)


def test_given_missing_tampered_or_wrong_bound_artifact_when_verified_then_integrity_failure(tmp_path: Path):
    relative, digest = _artifact(tmp_path)
    expected = dict(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo="example-org/example-repo",
        expected_issue=2054,
        expected_body_sha256="sha256:" + "c" * 64,
        expected_invocation_id="secure-invocation",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert (
        transport.verify_artifact(
            **{**expected, "artifact_relative": "2054/secure-invocation/attempt-002/missing.json"}
        )["status"]
        == "integrity_failure"
    )
    assert (
        transport.verify_artifact(**{**expected, "expected_repo": "other/repository"})["status"] == "integrity_failure"
    )
    artifact = tmp_path / relative
    artifact.write_bytes(b'{"schema":"REVIEWER_COMPACT_ARTIFACT_V2","schema":"duplicate"}')
    assert transport.verify_artifact(**expected)["status"] == "integrity_failure"


def test_given_non_regular_leaf_when_verified_then_integrity_failure(tmp_path: Path):
    leaf = tmp_path / "2054" / "secure-invocation" / "attempt-001" / "compact_review_result_v2.json"
    leaf.mkdir(parents=True)
    result = transport.verify_artifact(
        artifact_root=tmp_path,
        artifact_relative=str(leaf.relative_to(tmp_path)),
        expected_repo="example-org/example-repo",
        expected_issue=2054,
        expected_body_sha256="sha256:" + "c" * 64,
        expected_invocation_id="secure-invocation",
        expected_attempt=1,
        expected_sha256="sha256:" + "0" * 64,
    )
    assert result["status"] == "integrity_failure"
