"""
Tests for issue metadata mutation command ids (Issue #1284):
issue_body.update / issue_comment.publish / contract_snapshot.publish.

AC1  test_ac1_contract_snapshot_publish_from_root
AC2  test_ac2_input_file_outside_artifacts_subtree_fails_closed
AC3  test_ac3_binding_violations_rejected_before_mutation
AC4  test_ac4_readback_mismatch_not_success
AC6  test_ac6_tracked_diff_fails_executor
AC8  test_ac8_contract_snapshot_publisher_authority_fixed
AC9  test_ac9_issue_body_update_stale_write_prevented
AC10 test_ac10_per_command_input_schema_enforced
AC11 test_ac11_input_namespace_unified_to_artifacts
AC13 test_ac13_argv_only_shell_false_enforced
AC14 test_ac14_readback_idempotency_no_false_success
AC15 test_ac15_env_binding_optional_but_must_match
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as _exec  # noqa: I001
from controlled_skill_mutation_policy import (
    TRUSTED_REPO,
    COMMAND_ID_ISSUE_BODY_UPDATE,
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
)


def _sha(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _contract_snapshot_input(issue_number: int, **overrides) -> dict:
    """CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1 with all Blocker-4-required fields bound."""
    data = {
        "schema": "CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1",
        "issue_number": issue_number,
        "repo": TRUSTED_REPO,
        "target_issue_body_sha256": _sha("current issue body"),
        "expected_latest_contract_review_status": "go",
        "expected_contract_marker": "<!-- CONTRACT_REVIEW_MARKER -->",
        "operation_reason": "contract_snapshot_publish",
    }
    data.update(overrides)
    return data


@pytest.fixture(autouse=True)
def _scrub_ambient_env(monkeypatch):
    """Ambient GIT_* / LOOP_ISSUE_NUMBER must not leak into the tmp-repo and binding tests."""
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "LOOP_ISSUE_NUMBER",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def tmp_project(tmp_path):
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    pub_dir = tmp_path / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    pub_dir.mkdir(parents=True)
    create_issue_dir = tmp_path / ".claude" / "skills" / "create-issue" / "scripts"
    create_issue_dir.mkdir(parents=True)
    (create_issue_dir / "prose_boundary_policy.py").write_text("# stub\n")
    irl_dir = tmp_path / ".claude" / "skills" / "impl-review-loop" / "scripts"
    irl_dir.mkdir(parents=True)
    (irl_dir / "ensure_contract_snapshot.py").write_text("# stub\n")

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


def _write_input(tmp_project, issue_number, command_id, name, data):
    d = tmp_project / "artifacts" / str(issue_number) / "issue-metadata" / command_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(data))
    return f"artifacts/{issue_number}/issue-metadata/{command_id}/{name}"


# =============================================================================
# AC10 / AC11: namespace + schema
# =============================================================================


class TestNamespaceAndSchema:
    def test_ac11_input_namespace_unified_to_artifacts(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1284,
                "comment_body": "hi <!-- m --> ",
                "marker": "<!-- m -->",
            },
        )
        assert rel.startswith("artifacts/1284/issue-metadata/issue_comment.publish/")
        canonical, err = _exec._validate_and_resolve_input_file(
            rel, 1284, tmp_project, command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH
        )
        assert err == ""
        assert canonical is not None

    def test_ac2_input_file_outside_artifacts_subtree_fails_closed(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # File exists but outside the command-id-specific subtree.
        other_dir = tmp_project / "artifacts" / "1284"
        other_dir.mkdir(parents=True, exist_ok=True)
        (other_dir / "outside.json").write_text("{}")
        canonical, err = _exec._validate_and_resolve_input_file(
            "artifacts/1284/outside.json",
            1284,
            tmp_project,
            command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        )
        assert canonical is None
        assert "outside_issue_subtree" in err

    def test_ac10_per_command_input_schema_enforced(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 1284},
        )
        canonical, _ = _exec._validate_and_resolve_input_file(
            rel, 1284, tmp_project, command_id=COMMAND_ID_ISSUE_BODY_UPDATE
        )
        data, err = _exec._load_and_validate_input_json(
            canonical, 1284, COMMAND_ID_ISSUE_BODY_UPDATE
        )
        assert data is None
        assert "input_schema_mismatch" in err


# =============================================================================
# AC3: binding violations
# =============================================================================


class TestBindingViolations:
    def test_ac3_binding_violations_rejected_before_mutation(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1284",
                "--input-file",
                "/etc/passwd",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_wrong_repo_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1284",
                "--input-file",
                "artifacts/1284/issue-metadata/issue_comment.publish/x.json",
                "--repo",
                "evil/repo",
            ]
        )
        assert rc == 2

    def test_dotdot_traversal_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1284",
                "--input-file",
                "artifacts/1284/issue-metadata/issue_comment.publish/../../../x.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_unknown_command_id_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                "unknown.command",
                "--issue-number",
                "1284",
                "--input-file",
                "artifacts/1284/issue-metadata/unknown.command/x.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2


# =============================================================================
# AC15: env binding optional-but-matching for new command ids
# =============================================================================


class TestEnvBinding:
    def test_ac15_env_missing_allowed_for_new_command(self):
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1284)
        assert err == ""

    def test_ac15_env_present_and_matching_allowed(self, monkeypatch):
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1284)
        assert err == ""

    def test_ac15_env_present_and_mismatching_denied(self, monkeypatch):
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "9999")
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1284)
        assert "issue_number_mismatch" in err

    def test_env_binding_mandatory_command_ids_is_empty(self):
        """Issue #1873: termination_report.publish (the only member of
        ENV_BINDING_MANDATORY_COMMAND_IDS) was removed, so the set is empty
        and no command id currently requires LOOP_ISSUE_NUMBER."""
        from controlled_skill_mutation_policy import ENV_BINDING_MANDATORY_COMMAND_IDS

        assert ENV_BINDING_MANDATORY_COMMAND_IDS == frozenset()


# =============================================================================
# AC9: issue_body.update stale-write prevention
# =============================================================================


class TestIssueBodyUpdate:
    def test_ac9_issue_body_update_stale_write_prevented(self, tmp_project, monkeypatch, capsys):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        # Live readback disagrees with previous_body_sha256 → stale, must not mutate.
        with patch.object(  # noqa: SIM117
            _exec,
            "_fetch_issue_body_and_updated_at",
            return_value=("CURRENT DIFFERENT BODY", "2026-01-01T00:00:00Z", ""),
        ):
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                        "--json",
                    ]
                )
        assert rc == 1
        mock_patch.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["reason"].startswith("stale_precondition_body_sha256_mismatch")

    def test_ac9_partial_match_not_success(self, tmp_project, monkeypatch, capsys):
        """updatedAt matches but body hash doesn't (or vice versa) → still not success."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        with patch.object(  # noqa: SIM117
            _exec,
            "_fetch_issue_body_and_updated_at",
            return_value=("old body", "2026-01-02T99:99:99Z", ""),
        ):
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                        "--json",
                    ]
                )
        assert rc == 1
        mock_patch.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["reason"].startswith("stale_precondition_updated_at_mismatch")

    def test_issue_body_update_success_path(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        readbacks = iter(
            [
                ("old body", "2026-01-01T00:00:00Z", ""),
                (new_body, "2026-01-01T00:00:01Z", ""),
            ]
        )
        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_body_and_updated_at", side_effect=lambda *a, **k: next(readbacks)
        ):
            with patch.object(_exec, "_patch_issue_body", return_value=""):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_ISSUE_BODY_UPDATE,
                            "--issue-number",
                            "1284",
                            "--input-file",
                            rel,
                            "--repo",
                            TRUSTED_REPO,
                            "--json",
                        ]
                    )
        assert rc == 0


class TestIssueBodyUpdateMarkerAuthority:
    """Blocker 1: local marker is cache/audit only, never remote-mutation authority."""

    def _write_marker(
        self, tmp_project, issue_number, command_id, new_body_sha256, repo=TRUSTED_REPO
    ):
        mp = _exec._issue_metadata_marker_path(
            tmp_project, issue_number, command_id, "issue_body_update.marker.json"
        )
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_BODY_UPDATE_MARKER_V1",
                    "issue_number": issue_number,
                    "repo": repo,
                    "new_body_sha256": new_body_sha256,
                }
            )
        )
        return mp

    def test_issue_body_update_stale_marker_remote_body_changed_falls_to_stale_body_precondition_no_patch(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        self._write_marker(tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, _sha(new_body))
        with patch.object(  # noqa: SIM117
            _exec,
            "_fetch_issue_body_and_updated_at",
            return_value=("SOMEONE ELSE EDITED THIS", "2026-02-01T00:00:00Z", ""),
        ):
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                    ]
                )
        assert rc == 1
        mock_patch.assert_not_called()

    def test_issue_body_update_stale_marker_remote_already_new_returns_already_applied_no_patch(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        self._write_marker(tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, _sha(new_body))
        with patch.object(  # noqa: SIM117
            _exec,
            "_fetch_issue_body_and_updated_at",
            return_value=(new_body, "2026-02-01T00:00:00Z", ""),
        ):
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                        "--json",
                    ]
                )
        assert rc == 0
        mock_patch.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status_detail"] == "already_applied"
        assert payload["marker_state"] == "already_applied_remote_authority"

    def test_issue_body_update_stale_marker_remote_matches_previous_preconditions_patches_once(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        self._write_marker(tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, _sha("different body"))
        readbacks = iter(
            [
                ("old body", "2026-01-01T00:00:00Z", ""),
                (new_body, "2026-01-01T00:00:01Z", ""),
            ]
        )
        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_body_and_updated_at", side_effect=lambda *a, **k: next(readbacks)
        ):
            with patch.object(_exec, "_patch_issue_body", return_value="") as mock_patch:
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_ISSUE_BODY_UPDATE,
                            "--issue-number",
                            "1284",
                            "--input-file",
                            rel,
                            "--repo",
                            TRUSTED_REPO,
                            "--json",
                        ]
                    )
        assert rc == 0
        mock_patch.assert_called_once()
        payload = json.loads(capsys.readouterr().out)
        assert payload["marker_state"] == "stale_local_marker_recovered"

    def test_issue_body_update_stale_marker_remote_updated_at_changed_falls_to_stale_updated_at_no_patch(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        self._write_marker(tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, _sha("different body"))
        with patch.object(  # noqa: SIM117
            _exec,
            "_fetch_issue_body_and_updated_at",
            return_value=("old body", "2026-02-01T00:00:00Z", ""),
        ):
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                        "--json",
                    ]
                )
        assert rc == 1
        mock_patch.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["reason"].startswith("stale_precondition_updated_at_mismatch")

    def test_issue_body_update_marker_metadata_mismatch_still_denies_before_remote_readback(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        # Marker file claims a different repo -- must not be trusted.
        self._write_marker(
            tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, _sha(new_body), repo="evil/repo"
        )
        with patch.object(_exec, "_fetch_issue_body_and_updated_at") as mock_fetch:  # noqa: SIM117
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                        "--json",
                    ]
                )
        assert rc == 2
        mock_patch.assert_not_called()
        mock_fetch.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["reason"] == "issue_body_update_marker_metadata_mismatch"

    def test_issue_body_update_marker_issue_number_mismatch_still_denies_before_remote_readback(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, "issue_body_update.marker.json"
        )
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_BODY_UPDATE_MARKER_V1",
                    "issue_number": 9999,
                    "repo": TRUSTED_REPO,
                    "new_body_sha256": _sha(new_body),
                }
            )
        )
        with patch.object(_exec, "_fetch_issue_body_and_updated_at") as mock_fetch:  # noqa: SIM117
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                        "--json",
                    ]
                )
        assert rc == 2
        mock_patch.assert_not_called()
        mock_fetch.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["reason"] == "issue_body_update_marker_metadata_mismatch"

    def test_issue_body_update_corrupt_marker_still_denies_before_mutation(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_BODY_UPDATE,
            "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, "issue_body_update.marker.json"
        )
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("{not-json")
        with patch.object(_exec, "_fetch_issue_body_and_updated_at") as mock_fetch:  # noqa: SIM117
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main(
                    [
                        "--command-id",
                        COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number",
                        "1284",
                        "--input-file",
                        rel,
                        "--repo",
                        TRUSTED_REPO,
                    ]
                )
        assert rc == 2
        mock_patch.assert_not_called()
        mock_fetch.assert_not_called()


# =============================================================================
# AC4/AC14: readback mismatch not success (issue_comment.publish)
# =============================================================================


class TestIssueCommentPublish:
    def test_ac4_readback_mismatch_not_success(self, tmp_project, monkeypatch):
        """No remote marker present pre-mutation (proceed to post), but
        post-mutation readback fails -> not success."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1284,
                "comment_body": "hi <!-- marker-x -->",
                "marker": "<!-- marker-x -->",
            },
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_find_marker_matches", return_value=([], "")):
                    with patch.object(_exec, "_post_gh_comment", return_value=("url", "1", "")):
                        with patch.object(
                            _exec,
                            "_readback_by_marker_literal",
                            return_value={"error": "marker_not_found"},
                        ):
                            rc = _exec.main(
                                [
                                    "--command-id",
                                    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                    "--issue-number",
                                    "1284",
                                    "--input-file",
                                    rel,
                                    "--repo",
                                    TRUSTED_REPO,
                                ]
                            )
        assert rc == 1

    def test_ac14_pre_mutation_duplicate_denied_before_post(self, tmp_project, monkeypatch):
        """Blocker 3: remote already has >1 marker match -> deny BEFORE mutation.
        _post_gh_comment must never be called."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1284,
                "comment_body": "hi <!-- marker-x -->",
                "marker": "<!-- marker-x -->",
            },
        )
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
                                "1284",
                                "--input-file",
                                rel,
                                "--repo",
                                TRUSTED_REPO,
                            ]
                        )
        assert rc == 1
        mock_post.assert_not_called()

    def test_remote_marker_exists_local_marker_missing_post_not_called(
        self, tmp_project, monkeypatch
    ):
        """Blocker 3 additional test: remote already has the exact marker with
        matching body identity, local marker file is missing -> no-op success,
        _post_gh_comment not called."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        comment_body = "hi <!-- marker-x -->"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1284,
                "comment_body": comment_body,
                "marker": "<!-- marker-x -->",
            },
        )
        remote_comment = {"id": "42", "url": "https://ex/42", "body": comment_body}
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
                                "1284",
                                "--input-file",
                                rel,
                                "--repo",
                                TRUSTED_REPO,
                                "--json",
                            ]
                        )
        assert rc == 0
        mock_post.assert_not_called()
        mp = _exec._issue_metadata_marker_path(
            tmp_project, 1284, COMMAND_ID_ISSUE_COMMENT_PUBLISH, "issue_comment_publish.marker.json"
        )
        assert mp.exists()

    def test_remote_marker_body_sha256_mismatch_conflict_before_mutation(
        self, tmp_project, monkeypatch
    ):
        """Blocker 2/3: remote marker exists but body identity differs from the
        input's expected comment_body -> conflict BEFORE mutation, no post."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1284,
                "comment_body": "hi <!-- marker-x -->",
                "marker": "<!-- marker-x -->",
            },
        )
        remote_comment = {
            "id": "42",
            "url": "https://ex/42",
            "body": "DIFFERENT BODY <!-- marker-x -->",
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
                                "1284",
                                "--input-file",
                                rel,
                                "--repo",
                                TRUSTED_REPO,
                            ]
                        )
        assert rc == 1
        mock_post.assert_not_called()

    def test_issue_comment_publish_success_path(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        comment_body = "hi <!-- marker-x -->"
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1284,
                "comment_body": comment_body,
                "marker": "<!-- marker-x -->",
            },
        )
        import hashlib

        expected_sha = hashlib.sha256(comment_body.encode()).hexdigest()
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_find_marker_matches", return_value=([], "")):
                    with patch.object(
                        _exec, "_post_gh_comment", return_value=("https://ex", "1", "")
                    ):
                        with patch.object(
                            _exec,
                            "_readback_by_marker_literal",
                            return_value={
                                "comment_id": "1",
                                "comment_url": "https://ex",
                                "body_sha256": expected_sha,
                            },
                        ):
                            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                                rc = _exec.main(
                                    [
                                        "--command-id",
                                        COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                        "--issue-number",
                                        "1284",
                                        "--input-file",
                                        rel,
                                        "--repo",
                                        TRUSTED_REPO,
                                        "--json",
                                    ]
                                )
        assert rc == 0


# =============================================================================
# AC6: tracked diff fails executor
# =============================================================================


class TestTrackedDiff:
    def test_ac6_tracked_diff_fails_executor(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1284,
                "comment_body": "hi <!-- marker-x -->",
                "marker": "<!-- marker-x -->",
            },
        )
        import hashlib

        expected_sha = hashlib.sha256("hi <!-- marker-x -->".encode()).hexdigest()  # noqa: UP012
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_find_marker_matches", return_value=([], "")):
                    with patch.object(
                        _exec, "_post_gh_comment", return_value=("https://ex", "1", "")
                    ):
                        with patch.object(
                            _exec,
                            "_readback_by_marker_literal",
                            return_value={
                                "comment_id": "1",
                                "comment_url": "https://ex",
                                "body_sha256": expected_sha,
                            },
                        ):
                            with patch.object(
                                _exec,
                                "_check_no_tracked_changes",
                                return_value=["M:src/tracked_file.py"],
                            ):
                                rc = _exec.main(
                                    [
                                        "--command-id",
                                        COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                        "--issue-number",
                                        "1284",
                                        "--input-file",
                                        rel,
                                        "--repo",
                                        TRUSTED_REPO,
                                    ]
                                )
        assert rc == 1


# =============================================================================
# AC1/AC8: contract_snapshot.publish
# =============================================================================


class TestContractSnapshotPublish:
    def test_ac1_contract_snapshot_publish_from_root(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # No LOOP_ISSUE_NUMBER set — simulates root/default-branch execution
        # with no issue-specific worktree/session env (AC1/AC15).
        monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        pub_result = {
            "status": "ok",
            "contract_snapshot_url": "https://github.com/o/r/issues/1284#issuecomment-1",
            "post_status": "posted",
        }
        fake_proc = type(
            "P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0}
        )()
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_check_contract_snapshot_module_realpaths", return_value=[]
                ):
                    with patch.object(
                        _exec,
                        "_fetch_issue_body_and_updated_at",
                        return_value=("current issue body", "2026-01-01T00:00:00Z", ""),
                    ):
                        with patch("subprocess.run", return_value=fake_proc):
                            with patch.object(
                                _exec,
                                "_readback_contract_snapshot",
                                return_value={
                                    "comment_url": pub_result["contract_snapshot_url"],
                                    "remote_postcondition_verified": True,
                                },
                            ):
                                with patch.object(
                                    _exec, "_check_no_tracked_changes", return_value=[]
                                ):
                                    rc = _exec.main(
                                        [
                                            "--command-id",
                                            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                            "--issue-number",
                                            "1284",
                                            "--input-file",
                                            rel,
                                            "--repo",
                                            TRUSTED_REPO,
                                            "--json",
                                        ]
                                    )
        assert rc == 0

    def test_ac8_contract_snapshot_publisher_authority_fixed(self):
        assert _exec._ENSURE_CONTRACT_SNAPSHOT_REL == (
            ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"
        )

    def test_ac8_publisher_invoked_argv_list_shell_false(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        pub_result = {
            "status": "ok",
            "contract_snapshot_url": "https://ex",
            "post_status": "posted",
        }
        fake_proc = type(
            "P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0}
        )()
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return fake_proc

        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_check_contract_snapshot_module_realpaths", return_value=[]
                ):
                    with patch.object(
                        _exec,
                        "_fetch_issue_body_and_updated_at",
                        return_value=("current issue body", "2026-01-01T00:00:00Z", ""),
                    ):
                        with patch("subprocess.run", side_effect=_fake_run):
                            with patch.object(
                                _exec,
                                "_readback_contract_snapshot",
                                return_value={
                                    "comment_url": pub_result["contract_snapshot_url"],
                                    "remote_postcondition_verified": True,
                                },
                            ):
                                with patch.object(
                                    _exec, "_check_no_tracked_changes", return_value=[]
                                ):
                                    rc = _exec.main(
                                        [
                                            "--command-id",
                                            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                            "--issue-number",
                                            "1284",
                                            "--input-file",
                                            rel,
                                            "--repo",
                                            TRUSTED_REPO,
                                        ]
                                    )
        assert rc == 0
        assert isinstance(captured["cmd"], list)
        assert captured["kwargs"].get("shell") is False
        assert str(_exec.PROJECT_ROOT / _exec._ENSURE_CONTRACT_SNAPSHOT_REL) in captured["cmd"]
        # Blocker 5: sanitized env passed explicitly, no PYTHONPATH/PYTHONHOME leak.
        child_env = captured["kwargs"].get("env")
        assert child_env is not None
        assert "PYTHONPATH" not in child_env
        assert "PYTHONHOME" not in child_env
        assert child_env.get("GH_PROMPT_DISABLED") == "1"

    def test_contract_snapshot_publish_failure_not_success(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        pub_result = {"status": "human_judgment", "contract_snapshot_url": None}
        fake_proc = type(
            "P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0}
        )()
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_check_contract_snapshot_module_realpaths", return_value=[]
                ):
                    with patch.object(
                        _exec,
                        "_fetch_issue_body_and_updated_at",
                        return_value=("current issue body", "2026-01-01T00:00:00Z", ""),
                    ):
                        with patch("subprocess.run", return_value=fake_proc):
                            rc = _exec.main(
                                [
                                    "--command-id",
                                    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                    "--issue-number",
                                    "1284",
                                    "--input-file",
                                    rel,
                                    "--repo",
                                    TRUSTED_REPO,
                                ]
                            )
        assert rc == 1

    def test_contract_snapshot_publish_remote_marker_missing_not_success(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        pub_result = {
            "status": "ok",
            "contract_snapshot_url": "https://ex",
            "post_status": "posted",
        }
        fake_proc = type(
            "P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0}
        )()
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_check_contract_snapshot_module_realpaths", return_value=[]
                ):
                    with patch.object(
                        _exec,
                        "_fetch_issue_body_and_updated_at",
                        return_value=("current issue body", "now", ""),
                    ):
                        with patch("subprocess.run", return_value=fake_proc):
                            with patch.object(
                                _exec,
                                "_readback_contract_snapshot",
                                return_value={"error": "expected_contract_marker_match_count_0"},
                            ):
                                rc = _exec.main(
                                    [
                                        "--command-id",
                                        COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                        "--issue-number",
                                        "1284",
                                        "--input-file",
                                        rel,
                                        "--repo",
                                        TRUSTED_REPO,
                                    ]
                                )
        assert rc == 1

    def test_contract_snapshot_publish_missing_required_field_rc2(self, tmp_project, monkeypatch):
        """Blocker 4: {schema, issue_number}-only input is no longer sufficient
        to launch ensure_contract_snapshot.py --mode auto --post."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            {"schema": "CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1", "issue_number": 1284},
        )
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                "--issue-number",
                "1284",
                "--input-file",
                rel,
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_contract_snapshot_publish_missing_expected_status_rc2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        data = _contract_snapshot_input(1284)
        del data["expected_latest_contract_review_status"]
        rel = _write_input(tmp_project, 1284, COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH, "in.json", data)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                "--issue-number",
                "1284",
                "--input-file",
                rel,
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_contract_snapshot_publish_target_body_sha256_mismatch(self, tmp_project, monkeypatch):
        """target_issue_body_sha256 must match the live Issue body -- prevents
        publishing a snapshot against a stale/edited Issue body."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_check_contract_snapshot_module_realpaths", return_value=[]
                ):
                    with patch.object(
                        _exec,
                        "_fetch_issue_body_and_updated_at",
                        return_value=("EDITED BODY", "2026-01-02T00:00:00Z", ""),
                    ):
                        with patch("subprocess.run") as mock_run:
                            rc = _exec.main(
                                [
                                    "--command-id",
                                    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                    "--issue-number",
                                    "1284",
                                    "--input-file",
                                    rel,
                                    "--repo",
                                    TRUSTED_REPO,
                                ]
                            )
        assert rc == 1
        mock_run.assert_not_called()

    def test_contract_snapshot_publish_realpath_mismatch_denied(self, tmp_project, monkeypatch):
        """Blocker 5: ensure_contract_snapshot.py / run_contract_review_once.py /
        contract_review_result_parser.py module chain must resolve to canonical
        paths under project_root, mirroring the legacy publisher check."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch("subprocess.run") as mock_run:
                    rc = _exec.main(
                        [
                            "--command-id",
                            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                            "--issue-number",
                            "1284",
                            "--input-file",
                            rel,
                            "--repo",
                            TRUSTED_REPO,
                        ]
                    )
        # run_contract_review_once.py / contract_review_result_parser.py do not
        # exist in the tmp_project fixture -> module_missing -> deny.
        assert rc == 2
        mock_run.assert_not_called()

    def test_contract_snapshot_publish_ok_but_marker_missing_rc1(self, tmp_project, monkeypatch):
        """Publisher reports ok with a contract_snapshot_url, but the postcondition
        (no tracked changes outside the command's write root) fails -> not success."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        pub_result = {
            "status": "ok",
            "contract_snapshot_url": "https://ex",
            "post_status": "posted",
        }
        fake_proc = type(
            "P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0}
        )()
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_check_contract_snapshot_module_realpaths", return_value=[]
                ):
                    with patch.object(
                        _exec,
                        "_fetch_issue_body_and_updated_at",
                        return_value=("current issue body", "2026-01-01T00:00:00Z", ""),
                    ):
                        with patch("subprocess.run", return_value=fake_proc):
                            with patch.object(
                                _exec,
                                "_check_no_tracked_changes",
                                return_value=["??:artifacts/1284/unexpected.json"],
                            ):
                                rc = _exec.main(
                                    [
                                        "--command-id",
                                        COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                        "--issue-number",
                                        "1284",
                                        "--input-file",
                                        rel,
                                        "--repo",
                                        TRUSTED_REPO,
                                    ]
                                )
        assert rc == 1


# =============================================================================
# Issue #1459 review Blockers: legacy_refresh_duplicate_marker_deadlock /
# post_publish_live_body_not_revalidated / evaluator_missing_from_module_trust_chain
# =============================================================================

# Minimal but functionally real (not string-stub) replacements for
# ensure_contract_snapshot.py / contract_review_result_parser.py, used only by
# the tests below that exercise _readback_contract_snapshot's actual
# importlib-loaded module chain end to end. Full-fidelity coverage of
# is_go_current / parse_contract_review_results lives in
# test_ensure_contract_snapshot.py and contract_review_result_parser's own
# tests; this stub only needs to preserve the invariant these tests assert on
# (fresh comment id selection + live-body triple-hash agreement).
_ENSURE_MOD_FUNCTIONAL_STUB = """
def is_go_current(go_result, expected_body_sha256):
    if not isinstance(go_result, dict):
        return False
    inner = go_result.get("inner")
    if not isinstance(inner, dict):
        return False
    if inner.get("body_sha256") != expected_body_sha256:
        return False
    checks = inner.get("checks") or {}
    product_spec_check = checks.get("product_spec_check") or {}
    return product_spec_check.get("body_sha256") == expected_body_sha256
"""

_PARSER_MOD_FUNCTIONAL_STUB = """
import json
import re

_INNER_RE = re.compile(r"<!--TEST_INNER:(.*?)-->", re.S)


def parse_contract_review_results(comments, expected_issue_url=None):
    results = []
    for c in comments:
        body = c.get("body", "") or ""
        m = _INNER_RE.search(body)
        if not m:
            continue
        inner = json.loads(m.group(1))
        results.append({
            "comment_id": c.get("id"),
            "html_url": c.get("html_url"),
            "status": "go",
            "created_at": c.get("created_at"),
            "inner": inner,
            # #1475: this functional stub only exercises the readback/marker
            # flow (not trust-filtering itself, which is covered by the real
            # contract_review_result_parser.py unit tests), so every
            # comment produced by this fixture is treated as authoritative.
            "is_trusted_author": True,
        })
    return results


def find_latest_result(results, trusted_only=False):
    candidates = (
        [r for r in results if r.get("is_trusted_author")] if trusted_only else results
    )
    return candidates[-1] if candidates else None


def find_latest_go(results, trusted_only=False):
    go = [r for r in results if r.get("status") == "go"]
    if trusted_only:
        go = [r for r in go if r.get("is_trusted_author")]
    return go[-1] if go else None


def fetch_issue_comments(issue_number, repo):
    return [], None
"""


@pytest.fixture()
def tmp_project_functional(tmp_path):
    """Like tmp_project, but ensure_contract_snapshot.py / contract_review_result_parser.py
    are functionally real (not "# stub\\n") so _readback_contract_snapshot's
    importlib-loaded module chain actually runs end to end."""
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    irl_dir = tmp_path / ".claude" / "skills" / "impl-review-loop" / "scripts"
    irl_dir.mkdir(parents=True)
    (irl_dir / "ensure_contract_snapshot.py").write_text(_ENSURE_MOD_FUNCTIONAL_STUB)
    icr_dir = tmp_path / ".claude" / "skills" / "issue-contract-review" / "scripts"
    icr_dir.mkdir(parents=True)
    (icr_dir / "contract_review_result_parser.py").write_text(_PARSER_MOD_FUNCTIONAL_STUB)

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


# #1475 fix_delta P1 item 3 / P2 item 5: a variant of the functional parser
# stub where every parsed comment is untrusted (is_trusted_author: False),
# to prove _readback_contract_snapshot's find_latest_go(results,
# trusted_only=True) call actually rejects an untrusted-but-otherwise-valid
# posted comment, rather than merely asserting the call shape.
_PARSER_MOD_FUNCTIONAL_STUB_UNTRUSTED = _PARSER_MOD_FUNCTIONAL_STUB.replace(
    '"is_trusted_author": True,\n', '"is_trusted_author": False,\n'
).replace('"is_trusted_author": True', '"is_trusted_author": False')


@pytest.fixture()
def tmp_project_functional_untrusted(tmp_path):
    """Same as tmp_project_functional, but the parser stub reports every
    comment as untrusted -- proving the trusted_only=True gate in
    controlled_skill_mutation_exec.py's _readback_contract_snapshot is not a
    no-op."""
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    irl_dir = tmp_path / ".claude" / "skills" / "impl-review-loop" / "scripts"
    irl_dir.mkdir(parents=True)
    (irl_dir / "ensure_contract_snapshot.py").write_text(_ENSURE_MOD_FUNCTIONAL_STUB)
    icr_dir = tmp_path / ".claude" / "skills" / "issue-contract-review" / "scripts"
    icr_dir.mkdir(parents=True)
    (icr_dir / "contract_review_result_parser.py").write_text(_PARSER_MOD_FUNCTIONAL_STUB_UNTRUSTED)

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


class TestReadbackContractSnapshotRejectsUntrustedPublisher:
    """fix_delta P1 item 3: controlled_skill_mutation_exec.py is the actual
    controlled mutation boundary; it must apply trusted_only=True the same
    as every other consumer. This is a genuine end-to-end assertion (real
    find_latest_go execution against an untrusted-marked result set), not a
    mocked True/False return value standing in for the real check."""

    def test_readback_rejects_otherwise_valid_untrusted_comment(
        self, tmp_project_functional_untrusted, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project_functional_untrusted)
        body_text = "current issue body"
        body_sha256 = _sha(body_text)
        marker = (
            f"<!-- loop-protocol:contract-snapshot issue=1284 "
            f"body_sha256={body_sha256} schema=CONTRACT_REVIEW_RESULT_V1 -->"
        )
        inner = {
            "body_sha256": body_sha256,
            "checks": {"product_spec_check": {"body_sha256": body_sha256}},
        }
        fresh_url = "https://github.com/o/r/issues/1284#issuecomment-999"
        fresh_body = f"{marker}\n\n<!--TEST_INNER:{json.dumps(inner)}-->"

        def _fake_run(cmd, **kwargs):
            payload = {
                "id": 999,
                "html_url": fresh_url,
                "created_at": "t",
                "updated_at": "t",
                "body": fresh_body,
            }
            return type("P", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()

        with patch("subprocess.run", side_effect=_fake_run):  # noqa: SIM117
            with patch.object(
                _exec,
                "_fetch_issue_body_and_updated_at",
                return_value=(body_text, "now", ""),
            ):
                readback = _exec._readback_contract_snapshot(
                    marker,
                    1284,
                    TRUSTED_REPO,
                    "/bin/gh",
                    fresh_url,
                    body_sha256,
                )

        assert "error" in readback
        assert readback["error"] == "remote_contract_snapshot_not_current"


class TestReadbackContractSnapshotDuplicateMarker:
    """Blocker: legacy_refresh_duplicate_marker_deadlock. A legacy go comment
    can carry the exact same idempotency marker as the fresh comment just
    posted (the marker is derived from issue + body_sha256 + schema only, not
    from a per-post nonce). Readback must select the exact comment the
    publisher reported posting (by comment id parsed from its URL) and must
    never search the full comment list for a *uniquely* matching marker."""

    def test_selects_fresh_comment_by_id_ignoring_legacy_duplicate_marker(
        self, tmp_project_functional, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project_functional)
        body_text = "current issue body"
        body_sha256 = _sha(body_text)
        marker = (
            f"<!-- loop-protocol:contract-snapshot issue=1284 "
            f"body_sha256={body_sha256} schema=CONTRACT_REVIEW_RESULT_V1 -->"
        )
        inner = {
            "body_sha256": body_sha256,
            "checks": {"product_spec_check": {"body_sha256": body_sha256}},
        }
        fresh_url = "https://github.com/o/r/issues/1284#issuecomment-999"
        fresh_body = f"{marker}\n\n<!--TEST_INNER:{json.dumps(inner)}-->"

        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            # A legacy comment (e.g. id 111) carrying the identical marker text
            # exists on GitHub but this function must never issue a
            # list-all-comments call that would have to disambiguate between
            # them -- it must go straight to the id parsed from expected_url.
            assert cmd[0] == "/bin/gh" and cmd[1] == "api"
            assert cmd[2:5] == [
                "--hostname",
                "github.com",
                f"repos/{TRUSTED_REPO}/issues/comments/999",
            ], cmd
            payload = _single_comment_payload(
                id=999,
                html_url=fresh_url,
                created_at="2026-01-02T00:00:00Z",
                updated_at="2026-01-02T00:00:00Z",
                body=fresh_body,
            )
            return type("P", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()

        with patch("subprocess.run", side_effect=_fake_run):  # noqa: SIM117
            with patch.object(
                _exec,
                "_fetch_issue_body_and_updated_at",
                return_value=(body_text, "now", ""),
            ):
                readback = _exec._readback_contract_snapshot(
                    marker,
                    1284,
                    TRUSTED_REPO,
                    "/bin/gh",
                    fresh_url,
                    body_sha256,
                )

        assert readback.get("remote_postcondition_verified") is True
        assert readback["comment_id"] == 999
        assert readback["comment_url"] == fresh_url
        assert len(calls) == 1


class TestReadbackContractSnapshotLiveBodyRevalidation:
    """Blocker: post_publish_live_body_not_revalidated. The comment-level
    checks alone only prove the *posted comment* is bound to
    expected_body_sha256; they do not prove the live Issue body still matches
    that hash at readback time. A concurrent body edit between the pre-publish
    check and this readback must not be reported as success even though the
    POST already happened (mutation occurred, postcondition failed)."""

    def _fresh_comment(self, body_sha256: str, comment_id: int = 999):
        marker = (
            f"<!-- loop-protocol:contract-snapshot issue=1284 "
            f"body_sha256={body_sha256} schema=CONTRACT_REVIEW_RESULT_V1 -->"
        )
        inner = {
            "body_sha256": body_sha256,
            "checks": {"product_spec_check": {"body_sha256": body_sha256}},
        }
        url = f"https://github.com/o/r/issues/1284#issuecomment-{comment_id}"
        body = f"{marker}\n\n<!--TEST_INNER:{json.dumps(inner)}-->"
        return marker, url, body

    def test_live_body_changed_after_post_is_not_success(self, tmp_project_functional, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project_functional)
        body_sha256 = _sha("body at pre-publish check time")
        marker, fresh_url, fresh_body = self._fresh_comment(body_sha256)

        def _fake_run(cmd, **kwargs):
            payload = {
                "id": 999,
                "html_url": fresh_url,
                "created_at": "t",
                "updated_at": "t",
                "body": fresh_body,
            }
            return type("P", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()

        with patch("subprocess.run", side_effect=_fake_run):  # noqa: SIM117
            with patch.object(
                _exec,
                "_fetch_issue_body_and_updated_at",
                # A different (concurrently edited) live body at readback time.
                return_value=("body changed by a concurrent editor", "later", ""),
            ):
                readback = _exec._readback_contract_snapshot(
                    marker,
                    1284,
                    TRUSTED_REPO,
                    "/bin/gh",
                    fresh_url,
                    body_sha256,
                )

        assert "error" in readback
        assert readback["error"].startswith("failed_after_mutation:")
        # The POST already happened (remote side effect exists); evidence must
        # be preserved so the caller can reconcile the mutation, not lose it.
        assert readback["comment_id"] == 999
        assert readback["comment_url"] == fresh_url

    def test_contract_snapshot_publish_end_to_end_fails_closed_and_preserves_evidence(
        self, tmp_project_functional, monkeypatch
    ):
        """End-to-end through _run_contract_snapshot_publish: a live body
        change discovered only at readback time must surface as rc == 1
        (failed), never rc == 0, with the posted comment URL retained in the
        error evidence for human reconciliation."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project_functional)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        body_text = "current issue body"
        body_sha256 = _sha(body_text)
        marker, fresh_url, fresh_body = self._fresh_comment(body_sha256)
        rel = _write_input(
            tmp_project_functional,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(
                1284,
                target_issue_body_sha256=body_sha256,
                expected_contract_marker=marker,
            ),
        )
        pub_result = {"status": "ok", "contract_snapshot_url": fresh_url, "post_status": "posted"}
        fake_publisher_proc = type(
            "P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0}
        )()

        call_log = []
        issue_readback_calls = []

        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            if _exec._ENSURE_CONTRACT_SNAPSHOT_REL in " ".join(str(c) for c in cmd):
                return fake_publisher_proc
            if cmd[1:5] == ["api", "--hostname", "github.com", f"repos/{TRUSTED_REPO}/issues/1284"]:
                issue_readback_calls.append((cmd, kwargs))
                payload = {
                    "body": (
                        body_text
                        if len(issue_readback_calls) == 1
                        else "body changed by a concurrent editor"
                    ),
                    "updatedAt": f"t{len(issue_readback_calls) - 1}",
                }
                return type(
                    "P", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}
                )()
            payload = _single_comment_payload(
                id=999,
                html_url=fresh_url,
                created_at="t",
                updated_at="t",
                body=fresh_body,
            )
            return type("P", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()

        monkeypatch.setenv("GH_HOST", "attacker.example")
        monkeypatch.setenv("GH_REPO", "other/repo")
        monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/evil")
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec, "_check_contract_snapshot_module_realpaths", return_value=[]
                ):
                    with patch("subprocess.run", side_effect=_fake_run):
                        rc = _exec.main(
                            [
                                "--command-id",
                                COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                "--issue-number",
                                "1284",
                                "--input-file",
                                rel,
                                "--repo",
                                TRUSTED_REPO,
                                "--json",
                            ]
                        )
        assert rc == 1
        assert len(issue_readback_calls) == 2
        for cmd, kwargs in issue_readback_calls:
            assert cmd == [
                "/bin/gh",
                "api",
                "--hostname",
                "github.com",
                f"repos/{TRUSTED_REPO}/issues/1284",
                "--jq",
                "{body, updatedAt: .updated_at}",
            ]
            # Issue #2340 fix_delta P0-1: GH_CONFIG_DIR is the intentional
            # credential-carrier (kept so the launcher-shared gh credential
            # store is reachable) -- only GH_HOST / GH_REPO (redirection
            # surface, always explicitly --hostname pinned above) are
            # stripped now.
            for key in ("GH_HOST", "GH_REPO"):
                assert key not in kwargs["env"]


class TestModuleRealpathsEvaluatorTrustChain:
    """Blocker: evaluator_missing_from_module_trust_chain.
    ensure_contract_snapshot.py imports evaluate_product_spec_gate.py at
    module load time, so it must be part of the realpath-checked publisher
    module chain -- and path ancestry must be decided by Path.is_relative_to()
    against the resolved project root, not a raw str.startswith() prefix
    check (which also treats a sibling directory like "<root>-evil" as
    "under" "<root>" purely by string-prefix coincidence)."""

    def test_evaluator_included_in_trust_chain(self):
        assert _exec._EVALUATE_PRODUCT_SPEC_GATE_REL == (
            ".claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py"
        )

    def test_repo_external_symlink_evaluator_detected_and_denied(self, tmp_path):
        project_root = tmp_path / "repo"
        (project_root / ".claude/skills/impl-review-loop/scripts").mkdir(parents=True)
        (project_root / ".claude/skills/issue-contract-review/scripts").mkdir(parents=True)
        (project_root / _exec._ENSURE_CONTRACT_SNAPSHOT_REL).write_text("# stub\n")
        (project_root / _exec._RUN_CONTRACT_REVIEW_ONCE_REL).write_text("# stub\n")
        (project_root / _exec._CONTRACT_REVIEW_RESULT_PARSER_REL).write_text("# stub\n")

        # Sibling directory whose name path-prefix-collides with project_root
        # (e.g. ".../repo-evil" starts with the literal string ".../repo").
        # A raw str.startswith() ancestry check would incorrectly treat this
        # as "under" project_root; Path.is_relative_to() must not.
        evil_root = tmp_path / (project_root.name + "-evil")
        evil_root.mkdir()
        real_evaluator = evil_root / "evaluate_product_spec_gate.py"
        real_evaluator.write_text("# attacker-controlled\n")
        symlinked_evaluator = project_root / _exec._EVALUATE_PRODUCT_SPEC_GATE_REL
        symlinked_evaluator.symlink_to(real_evaluator)

        errors = _exec._check_contract_snapshot_module_realpaths(project_root)
        assert any("module_shadowing" in e and "evaluate_product_spec_gate.py" in e for e in errors)

    def test_missing_evaluator_denied(self, tmp_path):
        project_root = tmp_path / "repo"
        (project_root / ".claude/skills/impl-review-loop/scripts").mkdir(parents=True)
        (project_root / ".claude/skills/issue-contract-review/scripts").mkdir(parents=True)
        (project_root / _exec._ENSURE_CONTRACT_SNAPSHOT_REL).write_text("# stub\n")
        (project_root / _exec._RUN_CONTRACT_REVIEW_ONCE_REL).write_text("# stub\n")
        (project_root / _exec._CONTRACT_REVIEW_RESULT_PARSER_REL).write_text("# stub\n")
        # evaluate_product_spec_gate.py intentionally absent -- missing=deny.
        errors = _exec._check_contract_snapshot_module_realpaths(project_root)
        assert any("module_missing" in e and "evaluate_product_spec_gate.py" in e for e in errors)

    def test_publisher_not_launched_when_evaluator_realpath_check_fails(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project,
            1284,
            COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
            "in.json",
            _contract_snapshot_input(1284),
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):  # noqa: SIM117
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(
                    _exec,
                    "_check_contract_snapshot_module_realpaths",
                    return_value=[
                        "module_shadowing: evaluate_product_spec_gate.py resolved outside project_root"
                    ],
                ):
                    with patch("subprocess.run") as run_mock:
                        rc = _exec.main(
                            [
                                "--command-id",
                                COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                                "--issue-number",
                                "1284",
                                "--input-file",
                                rel,
                                "--repo",
                                TRUSTED_REPO,
                            ]
                        )
        # _fail(..., status="error") default -> rc == 2 (mirrors
        # test_contract_snapshot_publish_realpath_mismatch_denied).
        assert rc == 2
        run_mock.assert_not_called()


# =============================================================================
# Blocker 5: contract_snapshot.publish env sanitizer (unit-level)
# =============================================================================


class TestContractSnapshotEnvSanitizer:
    def test_metadata_sanitized_env_removes_pythonpath_pythonhome(self, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "/evil/path")
        monkeypatch.setenv("PYTHONHOME", "/evil/home")
        env = _exec._build_metadata_sanitized_env()
        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env

    def test_metadata_sanitized_env_removes_editor_browser(self, monkeypatch):
        monkeypatch.setenv("EDITOR", "vim")
        monkeypatch.setenv("VISUAL", "vim")
        monkeypatch.setenv("BROWSER", "firefox")
        monkeypatch.setenv("GH_EDITOR", "vim")
        env = _exec._build_metadata_sanitized_env()
        assert "EDITOR" not in env
        assert "VISUAL" not in env
        assert "BROWSER" not in env
        assert "GH_EDITOR" not in env

    def test_metadata_sanitized_env_disables_prompts(self):
        env = _exec._build_metadata_sanitized_env()
        assert env.get("GH_PROMPT_DISABLED") == "1"
        assert env.get("GH_NO_UPDATE_NOTIFIER") == "1"


# =============================================================================
# Issue #1664: canonical identity projection for single-comment readback
# =============================================================================

_SINGLE_COMMENT_ID = 5015599730
_SINGLE_COMMENT_URL = (
    f"https://github.com/{TRUSTED_REPO}/issues/1649#issuecomment-{_SINGLE_COMMENT_ID}"
)


def _single_comment_payload(**overrides):
    payload = {
        "id": _SINGLE_COMMENT_ID,
        "html_url": _SINGLE_COMMENT_URL,
        "created_at": "2026-07-20T00:00:00Z",
        "updated_at": "2026-07-20T00:00:00Z",
        "body": "snapshot body",
        "author": "squne121",
        "author_id": 63350259,
        "author_type": "User",
        "author_association": "OWNER",
    }
    payload.update(overrides)
    return payload


def _load_single_comment_production_parser():
    import importlib.util

    parser_path = (
        _GUARDS_DIR.parent.parent
        / ".claude/skills/issue-contract-review/scripts/contract_review_result_parser.py"
    )
    spec = importlib.util.spec_from_file_location("issue_1664_production_parser", parser_path)
    assert spec is not None and spec.loader is not None
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    return parser


class TestSingleCommentCanonicalReadback:
    def test_single_comment_canonical_projection(self):
        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return type(
                "P",
                (),
                {"returncode": 0, "stdout": json.dumps(_single_comment_payload()), "stderr": ""},
            )()

        with patch("subprocess.run", side_effect=_fake_run):
            result = _exec._fetch_single_comment_by_id(
                str(_SINGLE_COMMENT_ID), TRUSTED_REPO, "/bin/gh"
            )

        assert result == {"comment": _single_comment_payload()}
        assert calls[0][0] == [
            "/bin/gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{TRUSTED_REPO}/issues/comments/{_SINGLE_COMMENT_ID}",
            "--jq",
            (
                "{id, html_url, created_at, updated_at, body, "
                "author: .user.login, author_id: .user.id, "
                "author_type: .user.type, author_association}"
            ),
        ]
        assert calls[0][1]["env"]["GH_PROMPT_DISABLED"] == "1"
        assert calls[0][1]["env"]["GH_NO_UPDATE_NOTIFIER"] == "1"

    def test_single_comment_production_parser_accepts_trusted_snapshot(self):
        parser = _load_single_comment_production_parser()
        issue_url = f"https://github.com/{TRUSTED_REPO}/issues/1649"
        body_sha = _sha("current issue body")
        fingerprint_json = json.dumps(
            {
                "issue_number": 1649,
                "contract_source_kind": "issue_comment",
                "contract_source_id": str(_SINGLE_COMMENT_ID),
                "contract_body_sha256": body_sha,
                "allowed_paths_normalized_sha256": "a" * 64,
                "base_ref": "main",
                "base_sha_at_snapshot": "b" * 40,
            },
            separators=(",", ":"),
        )
        snapshot_body = "\n".join(
            [
                "```yaml",
                "CONTRACT_REVIEW_RESULT_V1:",
                "  status: go",
                "  generated_by: issue-contract-review",
                f'  issue_url: "{issue_url}"',
                '  generated_at: "2026-07-20T00:00:00Z"',
                f'  body_sha256: "{body_sha}"',
                f"  expected_contract_fingerprint: '{fingerprint_json}'",
                "```",
            ]
        )

        results = parser.parse_contract_review_results(
            [_single_comment_payload(body=snapshot_body)], issue_url
        )

        assert len(results) == 1
        assert results[0]["is_trusted_author"] is True
        assert results[0]["is_fingerprint_ready"] is True
        assert parser.find_latest_authoritative_go(results) == results[0]

    @pytest.mark.parametrize(
        ("field", "invalid_value"),
        [
            ("author", None),
            ("author", 63350259),
            ("author_id", None),
            ("author_id", "63350259"),
            ("author_id", True),
            ("author_type", None),
            ("author_type", 1),
            ("author_association", None),
            ("author_association", 1),
        ],
    )
    def test_single_comment_missing_identity_fails_closed(self, field, invalid_value):
        parser = _load_single_comment_production_parser()
        comment = _single_comment_payload()
        comment[field] = invalid_value

        assert (
            parser.is_trusted_snapshot_author(
                comment.get("author"),
                comment.get("author_association"),
                author_id=comment.get("author_id"),
                author_type=comment.get("author_type"),
            )
            is False
        )

    @pytest.mark.parametrize(
        "override",
        [
            {"author_id": 63350260},
            {"author": "unexpected-login"},
            {"author_type": "Organization"},
            {"author_association": "MEMBER"},
        ],
    )
    def test_single_comment_identity_tuple_mismatch_fails_closed(self, override):
        parser = _load_single_comment_production_parser()
        comment = _single_comment_payload(**override)

        assert (
            parser.is_trusted_snapshot_author(
                comment["author"],
                comment["author_association"],
                author_id=comment["author_id"],
                author_type=comment["author_type"],
            )
            is False
        )

    def test_single_comment_readback_binds_hostname_and_sanitized_env(self):
        # Issue #2340 fix_delta P0-1: GH_HOST / GH_REPO are redirection-
        # surface noise (always explicitly --hostname pinned above) and are
        # still stripped. GH_CONFIG_DIR is the intentional credential
        # carrier (kept so the launcher-shared gh credential store is
        # reachable) and is asserted PRESERVED below, not stripped.
        noise_env = {
            "GH_HOST": "attacker.example",
            "GH_REPO": "other/repo",
        }
        preserved_env = {
            "GH_CONFIG_DIR": "/fake/native/gh/config",
        }
        hostile_env = {**noise_env, **preserved_env}
        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps({"body": "current body", "updatedAt": "now"}),
                    "stderr": "",
                },
            )()

        with patch.dict(os.environ, hostile_env, clear=False):  # noqa: SIM117
            with patch("subprocess.run", side_effect=_fake_run):
                body, updated_at, error = _exec._fetch_issue_body_and_updated_at(
                    1649, TRUSTED_REPO, "/bin/gh"
                )

        assert (body, updated_at, error) == ("current body", "now", "")
        assert calls[0][0] == [
            "/bin/gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{TRUSTED_REPO}/issues/1649",
            "--jq",
            "{body, updatedAt: .updated_at}",
        ]
        child_env = calls[0][1]["env"]
        for key in noise_env:
            assert key not in child_env
        for key, value in preserved_env.items():
            assert child_env[key] == value
        assert child_env["GH_PROMPT_DISABLED"] == "1"
        assert child_env["GH_NO_UPDATE_NOTIFIER"] == "1"

    def test_single_comment_identity_fixture_preserves_existing_safeguards(
        self, tmp_project_functional, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project_functional)
        body_text = "current issue body"
        body_sha256 = _sha(body_text)
        marker = (
            f"<!-- loop-protocol:contract-snapshot issue=1649 "
            f"body_sha256={body_sha256} schema=CONTRACT_REVIEW_RESULT_V1 -->"
        )
        inner = {
            "body_sha256": body_sha256,
            "checks": {"product_spec_check": {"body_sha256": body_sha256}},
        }
        issue_url = f"https://github.com/{TRUSTED_REPO}/issues/1649"
        fingerprint_json = json.dumps(
            {
                "issue_number": 1649,
                "contract_source_kind": "issue_comment",
                "contract_source_id": str(_SINGLE_COMMENT_ID),
                "contract_body_sha256": body_sha256,
                "allowed_paths_normalized_sha256": "a" * 64,
                "base_ref": "main",
                "base_sha_at_snapshot": "b" * 40,
            },
            separators=(",", ":"),
        )
        snapshot_body = "\n".join(
            [
                "```yaml",
                "CONTRACT_REVIEW_RESULT_V1:",
                "  status: go",
                "  generated_by: issue-contract-review",
                f'  issue_url: "{issue_url}"',
                '  generated_at: "2026-07-20T00:00:00Z"',
                f'  body_sha256: "{body_sha256}"',
                f"  expected_contract_fingerprint: '{fingerprint_json}'",
                "```",
            ]
        )
        body = f"{marker}\n\n{snapshot_body}\n\n<!--TEST_INNER:{json.dumps(inner)}-->"
        comment = _single_comment_payload(body=body)
        parser = _load_single_comment_production_parser()
        results = parser.parse_contract_review_results([comment], issue_url)
        assert len(results) == 1
        assert results[0]["is_trusted_author"] is True
        assert results[0]["is_fingerprint_ready"] is True
        assert parser.find_latest_authoritative_go(results) == results[0]

        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(comment),
                    "stderr": "",
                },
            )()

        with patch("subprocess.run", side_effect=_fake_run):  # noqa: SIM117
            with patch.object(
                _exec, "_fetch_issue_body_and_updated_at", return_value=(body_text, "now", "")
            ):
                result = _exec._readback_contract_snapshot(
                    marker,
                    1649,
                    TRUSTED_REPO,
                    "/bin/gh",
                    _SINGLE_COMMENT_URL,
                    body_sha256,
                )

        assert result["remote_postcondition_verified"] is True
        assert result["comment_id"] == _SINGLE_COMMENT_ID
        assert len(calls) == 1
        assert calls[0][0][4] == f"repos/{TRUSTED_REPO}/issues/comments/{_SINGLE_COMMENT_ID}"
        assert not any(
            cmd[4] == f"repos/{TRUSTED_REPO}/issues/1649/comments" for cmd, _kwargs in calls
        )


# =============================================================================
# AC13: argv-only / shell=False enforced at policy layer
# =============================================================================


class TestArgvOnly:
    def test_ac13_argv_only_shell_false_enforced(self, tmp_path):
        from controlled_skill_mutation_policy import is_controlled_skill_mutation_exec_command

        executor_dir = tmp_path / "scripts" / "agent-guards"
        executor_dir.mkdir(parents=True)
        (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")

        good = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
            "--command-id issue_body.update --issue-number 1284 "
            "--input-file artifacts/1284/issue-metadata/issue_body.update/in.json "
            f"--repo {TRUSTED_REPO}"
        )
        assert is_controlled_skill_mutation_exec_command(good, str(tmp_path)) is True

        bash_wrapped = f"bash -c '{good}'"
        assert is_controlled_skill_mutation_exec_command(bash_wrapped, str(tmp_path)) is False

        dup_flags = good + " --command-id issue_body.update"
        assert is_controlled_skill_mutation_exec_command(dup_flags, str(tmp_path)) is False

        eq_form = good.replace("--command-id issue_body.update", "--command-id=issue_body.update")
        assert is_controlled_skill_mutation_exec_command(eq_form, str(tmp_path)) is False
