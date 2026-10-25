"""
Tests for controlled_skill_mutation_exec.py generic executor mechanism
(command_id whitelist / repo binding / issue binding / input-file binding /
input-JSON field validation / postcondition no-tracked-changes) plus the
issue_dependency.remove command id (Issue #1632 / #1667).

Issue #1873 removed the legacy termination_report.publish and pr_review.publish
command ids (and their dedicated marker/readback/module-realpath machinery).
The generic executor-mechanism tests below therefore exercise the still-live
issue_comment.publish command id as their representative fixture value instead
of the retired termination_report.publish -- the mechanism under test
(command_id whitelist, repo binding, git-remote binding, issue binding,
input-file binding, input-JSON validation, postcondition) is unchanged by that
retirement. Success-path / marker-authority / readback coverage for
issue_comment.publish itself already lives in
test_controlled_issue_metadata_exec.py (TestIssueCommentPublish) and is not
duplicated here; the classes below focus on the low-level, command-id-agnostic
negative fixtures (symlink/hardlink input-file components, malformed input
JSON, non-digit LOOP_ISSUE_NUMBER, generic postcondition checks) that were
previously only exercised through the now-retired termination_report.publish
fixture value.

Tests:
- AC8:  command_id validation (whitelist / unknown command id)
- AC10: repo validation (only TRUSTED_REPO)
- AC12: input-file validation (must be in artifact subtree, no symlinks, no hardlinks)
- AC10: input-file JSON validation (schema + issue_number field cross-check)
- AC15: LOOP_ISSUE_NUMBER binding (optional-but-matching; non-digit denied)
- AC14: postcondition (no tracked changes) -- generic unit-level coverage
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _GUARDS_DIR.parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as _exec  # noqa: I001
from controlled_skill_mutation_policy import (
    TRUSTED_REPO,
    COMMAND_ID_ISSUE_BODY_UPDATE,
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
)


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


class TestGraphqlCallDiagnosticRedaction:
    def test_errors_redacts_before_truncation_when_token_crosses_boundary(self):
        fake_token = "ghp_" + "A" * 36
        errors = [{"message": "x" * 275 + fake_token}]
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"errors": errors}),
            stderr="",
        )

        with patch.object(_exec.subprocess, "run", return_value=completed):
            data, error = _exec._graphql_call("gh", {}, "query Test", {})

        assert data is None
        prefix = "gh_api_graphql_errors: "
        assert error.startswith(prefix)
        assert len(error[len(prefix) :]) == 300
        assert fake_token not in error
        assert "ghp_" not in error
        assert "[REDACTED]" in error

    def test_response_parse_exception_redacts_secret_like_token(self):
        fake_token = "ghp_" + "B" * 36
        completed = SimpleNamespace(returncode=0, stdout="not-json", stderr="")

        with (
            patch.object(_exec.subprocess, "run", return_value=completed),
            patch.object(_exec.json, "loads", side_effect=ValueError(f"bad response {fake_token}")),
        ):
            data, error = _exec._graphql_call("gh", {}, "query Test", {})

        assert data is None
        assert fake_token not in error
        assert "[REDACTED]" in error

    def test_outer_subprocess_exception_redacts_secret_like_token(self):
        fake_token = "ghp_" + "C" * 36

        with patch.object(
            _exec.subprocess,
            "run",
            side_effect=RuntimeError(f"subprocess failure {fake_token}"),
        ):
            data, error = _exec._graphql_call("gh", {}, "query Test", {})

        assert data is None
        assert fake_token not in error
        assert "[REDACTED]" in error


# Issue #2718 AC2 / AC3 / AC4: `_redact_secret_like_tokens()` /
# `_graphql_call()` coverage for the new-format `ghs_<APP_ID>_<JWT>` GitHub
# App installation token, plus a regression fixture pinning that classic
# `gh[oprsu]_...` token redaction -- including the classic short `ghs_`
# form -- is unchanged. These are module-level functions (not methods) so
# their bare pytest node ids match the Issue's Verification Commands.


def test_redact_secret_like_tokens_fully_redacts_new_installation_token():
    """AC2: a new-format token (`ghs_<APP_ID>_<JWT header>.<payload>.<sig>`)
    must be replaced by exactly one `[REDACTED]` -- no APP_ID or JWT segment
    fragment may survive."""
    app_id = "123456789"
    jwt_header = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
    jwt_payload = "eyJpc3MiOiJodHRwczovL2FwaS5naXRodWIuY29tIiwiaWF0IjoxNzAwMDAwMDAwfQ"
    jwt_signature = "AbCdEf-123_XyZ9876543210"
    new_token = f"ghs_{app_id}_{jwt_header}.{jwt_payload}.{jwt_signature}"

    result = _exec._redact_secret_like_tokens(f"before {new_token} after")

    assert result == "before [REDACTED] after"
    assert app_id not in result
    assert jwt_header not in result
    assert jwt_payload not in result
    assert jwt_signature not in result


def test_redact_secret_like_tokens_preserves_classic_ghs_token_redaction():
    """AC3: the classic short `ghs_[A-Za-z0-9]{20,}` token form must still
    be fully redacted (no behavior regression from the new AC2 pattern)."""
    classic_ghs_token = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2"

    result = _exec._redact_secret_like_tokens(f"before {classic_ghs_token} after")

    assert result == "before [REDACTED] after"
    assert classic_ghs_token not in result
    assert "ghs_" not in result


def test_graphql_call_exception_redacts_new_installation_token():
    """AC4: the new-format token must be fully redacted through the same
    production call path (`_graphql_call`'s outer-exception branch) as
    `TestGraphqlCallDiagnosticRedaction.test_outer_subprocess_exception_redacts_secret_like_token`
    above -- no APP_ID/JWT fragment may survive in the returned
    diagnostic."""
    app_id = "987654321"
    jwt_header = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
    jwt_payload = "eyJpc3MiOiJodHRwczovL2FwaS5naXRodWIuY29tIiwiaWF0IjoxNzAwMDAwMDAwfQ"
    jwt_signature = "AbCdEf-123_XyZ9876543210"
    new_token = f"ghs_{app_id}_{jwt_header}.{jwt_payload}.{jwt_signature}"

    with patch.object(
        _exec.subprocess,
        "run",
        side_effect=RuntimeError(f"subprocess failure {new_token}"),
    ):
        data, error = _exec._graphql_call("gh", {}, "query Test", {})

    assert data is None
    assert new_token not in error
    assert app_id not in error
    assert jwt_header not in error
    assert jwt_payload not in error
    assert jwt_signature not in error
    assert "[REDACTED]" in error


def test_redact_secret_like_tokens_short_ghs_string_below_new_threshold_not_matched_by_new_pattern():
    """Regression pin for the PR #2722 review fix: the new-format `ghs_`
    pattern requires >= 36 chars after the prefix (not the originally-shipped
    >= 20), to avoid over-broadly redacting short, non-token strings that
    happen to start with `ghs_`. A 24-char body (matches the old `{20,}`
    threshold but not the new `{36,}` one) must fall through to the classic
    `gh[oprsu]_[A-Za-z0-9]{20,}` pattern instead -- it is still fully
    redacted overall, but not via the `ghs_`-specific pattern."""
    short_ghs_string = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2"  # 24 chars, alnum-only
    assert not _exec._SECRET_LIKE_PATTERNS[0].search(short_ghs_string)

    result = _exec._redact_secret_like_tokens(f"before {short_ghs_string} after")

    assert result == "before [REDACTED] after"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def tmp_project(tmp_path):
    """Create minimal project structure for executor tests."""
    # Create executor (so PROJECT_ROOT is set correctly via monkeypatching)
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    # Create the issue_comment.publish input namespace with a valid fixture.
    artifact_dir = (
        tmp_path / "artifacts" / "1166" / "issue-metadata" / COMMAND_ID_ISSUE_COMMENT_PUBLISH
    )
    artifact_dir.mkdir(parents=True)
    input_file = artifact_dir / "issue_comment_publish_input.json"
    input_file.write_text(
        json.dumps(
            {
                "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                "issue_number": 1166,
                "comment_body": "status update <!-- marker-1166 -->",
                "marker": "<!-- marker-1166 -->",
            }
        )
    )
    # Make a git repo with correct remote so _verify_git_remote_origin passes
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


_INPUT_REL = f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish_input.json"


# =============================================================================
# AC8: command_id validation
# =============================================================================


class TestCommandIdValidation:
    def test_valid_command_id_is_in_whitelist(self):
        from controlled_skill_mutation_policy import ALL_COMMAND_IDS

        assert COMMAND_ID_ISSUE_COMMENT_PUBLISH in ALL_COMMAND_IDS

    def test_unknown_command_id_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                "unknown.command",
                "--issue-number",
                "1166",
                "--input-file",
                _INPUT_REL,
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2


# =============================================================================
# AC10: repo validation
# =============================================================================


class TestRepoValidation:
    def test_wrong_repo_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                _INPUT_REL,
                "--repo",
                "evil-org/hijack-repo",
            ]
        )
        assert rc == 2


# =============================================================================
# AC12: input-file validation
# =============================================================================


class TestInputFileValidation:
    def test_file_not_found_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/nonexistent.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_file_outside_artifact_subtree_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Create a file outside the issue/command-id artifact subtree
        bad_dir = tmp_project / "tmp"
        bad_dir.mkdir(parents=True)
        (bad_dir / "evil.json").write_text(
            json.dumps(
                {
                    "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                    "issue_number": 1166,
                }
            )
        )
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                "tmp/evil.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_validate_and_resolve_input_file_fn_passes_for_valid(self, tmp_project):
        canonical, err = _exec._validate_and_resolve_input_file(
            _INPUT_REL, 1166, tmp_project, command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH
        )
        assert err == ""
        assert canonical is not None
        assert canonical.exists()

    def test_validate_and_resolve_input_file_fn_fails_for_wrong_issue(self, tmp_project):
        canonical, err = _exec._validate_and_resolve_input_file(
            _INPUT_REL, 9999, tmp_project, command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH
        )
        assert err != ""
        assert canonical is None


# =============================================================================
# P0-1 + P0-3: _validate_and_resolve_input_file negative fixtures
# =============================================================================


class TestInputFileNegativeFixtures:
    def test_absolute_path_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        abs_path = str(tmp_project / _INPUT_REL)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                abs_path,
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_dotdot_traversal_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/../{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish_input.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_symlink_component_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Create a symlink directory in the path
        link = tmp_project / "artifacts" / "link_to_1166"
        link.symlink_to(tmp_project / "artifacts" / "1166")
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                f"artifacts/link_to_1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish_input.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_hardlink_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        original = tmp_project / _INPUT_REL
        hardlink = original.parent / "hardlink_input.json"
        os.link(str(original), str(hardlink))
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/hardlink_input.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2


# =============================================================================
# P0-2: Input JSON validation
# =============================================================================


def _issue_metadata_dir(tmp_project):
    return tmp_project / "artifacts" / "1166" / "issue-metadata" / COMMAND_ID_ISSUE_COMMENT_PUBLISH


class TestInputJsonValidation:
    def test_missing_issue_number_in_json_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        bad_input = _issue_metadata_dir(tmp_project) / "bad_input.json"
        bad_input.write_text(json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1"}))
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/bad_input.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_issue_number_mismatch_in_json_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        bad_input = _issue_metadata_dir(tmp_project) / "mismatch_input.json"
        bad_input.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                    "issue_number": 9999,
                }
            )
        )
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/mismatch_input.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_wrong_schema_in_json_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        bad_input = _issue_metadata_dir(tmp_project) / "wrong_schema.json"
        bad_input.write_text(
            json.dumps(
                {
                    "schema": "WRONG_SCHEMA_V1",
                    "issue_number": 1166,
                }
            )
        )
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/wrong_schema.json",
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_valid_json_passes_validation(self, tmp_project):
        canonical = tmp_project / _INPUT_REL
        data, err = _exec._load_and_validate_input_json(
            canonical, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH
        )
        assert err == ""
        assert data is not None

    def test_missing_issue_number_fails_validation(self, tmp_project):
        f = _issue_metadata_dir(tmp_project) / "no_issue.json"
        f.write_text(json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1"}))
        data, err = _exec._load_and_validate_input_json(f, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH)
        assert data is None
        assert "input_issue_number_missing" in err

    def test_issue_mismatch_fails_validation(self, tmp_project):
        f = _issue_metadata_dir(tmp_project) / "mismatch.json"
        f.write_text(json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 9999}))
        data, err = _exec._load_and_validate_input_json(f, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH)
        assert data is None
        assert "input_issue_number_mismatch" in err

    def test_non_int_issue_number_fails_validation(self, tmp_project):
        f = _issue_metadata_dir(tmp_project) / "non_int.json"
        f.write_text(
            json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": "1166"})
        )
        data, err = _exec._load_and_validate_input_json(f, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH)
        assert data is None
        assert "input_issue_number_not_int" in err


# =============================================================================
# AC15: LOOP_ISSUE_NUMBER binding (optional-but-matching; Issue #1873 removed
# the only mandatory-binding command id, termination_report.publish, so a
# missing env var is now allowed for every remaining command id -- see
# test_controlled_issue_metadata_exec.py::TestEnvBinding for the
# missing/matching/mismatching coverage). The non-digit fixture below is not
# covered elsewhere.
# =============================================================================


class TestLoopIssueNumberBinding:
    def test_non_digit_loop_issue_number_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "not-a-number")
        rc = _exec.main(
            [
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                "1166",
                "--input-file",
                _INPUT_REL,
                "--repo",
                TRUSTED_REPO,
            ]
        )
        assert rc == 2

    def test_non_digit_loop_issue_number_denied_unit(self, monkeypatch):
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "not-a-number")
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1166)
        assert "loop_issue_number_env_not_digit" in err

    def test_missing_loop_issue_number_allowed(self, tmp_project, monkeypatch):
        """LOOP_ISSUE_NUMBER is optional for every live command id (Issue #1873
        removed the only mandatory member, termination_report.publish)."""
        import hashlib

        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
        comment_body = "status update <!-- marker-1166 -->"
        expected_body_sha256 = hashlib.sha256(comment_body.encode()).hexdigest()
        with patch.object(_exec, "_find_marker_matches", return_value=([], "")):  # noqa: SIM117
            with patch.object(_exec, "_post_gh_comment", return_value=("https://ex", "c1", "")):
                with patch.object(
                    _exec,
                    "_readback_by_marker_literal",
                    return_value={
                        "comment_id": "c1",
                        "comment_url": "https://ex",
                        "body_sha256": expected_body_sha256,
                    },
                ):
                    with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                        rc = _exec.main(
                            [
                                "--command-id",
                                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                "--issue-number",
                                "1166",
                                "--input-file",
                                _INPUT_REL,
                                "--repo",
                                TRUSTED_REPO,
                            ]
                        )
        assert rc == 0


# =============================================================================
# P1-4: Postcondition -- no tracked changes (generic, command-id-agnostic
# unit-level coverage; TestTrackedDiff in test_controlled_issue_metadata_exec.py
# covers the full main() flow for issue_comment.publish).
# =============================================================================


class TestPostconditionExtended:
    def test_check_no_tracked_changes_clean_repo(self, tmp_project):
        """In a clean git repo, no violations for artifacts/1166/ files."""
        violations = _exec._check_no_tracked_changes(tmp_project, 1166)
        assert isinstance(violations, list)

    def test_artifacts_allowed_prefix_not_flagged(self, tmp_project):
        """Untracked artifacts/1166/ files are not flagged as violations."""
        violations = _exec._check_no_tracked_changes(tmp_project, 1166)
        for v in violations:
            assert "artifacts/1166/" not in v, f"Unexpected violation: {v}"

    def test_command_id_scoped_prefix_isolates_sibling_namespace(self, tmp_project):
        """Issue #1284 Blocker 6: a write in a sibling command-id namespace
        must NOT be allowed by a command-id-scoped prefix."""
        sibling_dir = tmp_project / "artifacts" / "1166" / "issue-metadata" / "issue_body.update"
        sibling_dir.mkdir(parents=True, exist_ok=True)
        (sibling_dir / "unexpected.json").write_text("{}")
        allowed_prefix = f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/"
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, allowed_prefix)
        assert any("issue_body.update/unexpected.json" in v for v in violations)

    # -- Issue #2163: metadata-snapshot postcondition comparison -------------

    def test_postcondition_ignores_preexisting_untracked_file_no_content_change(self, tmp_project):
        """AC2: an untracked file that already existed before the mutation began,
        and whose content is unchanged during the mutation, must NOT be reported
        as postcondition_tracked_changes_detected (false positive fix -- the
        previous bare `git status` line set diff could not distinguish this from
        a genuinely new/changed file since the file is untracked either way)."""
        preexisting = tmp_project / "investigation_style_artifact.md"
        preexisting.write_text("unchanged content")
        write_root = "artifacts/1166/"
        pre_snapshot, pre_err = _exec._capture_pre_mutation_snapshot(tmp_project, 1166, write_root)
        assert pre_err is None
        assert "investigation_style_artifact.md" in pre_snapshot.candidate_paths

        # Nothing touches the file during the (simulated) mutation.
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, pre_snapshot)
        assert not any("investigation_style_artifact.md" in v for v in violations)

    def test_postcondition_detects_content_change_to_preexisting_file(self, tmp_project):
        """AC3: an untracked file that already existed before the mutation began,
        but whose content WAS overwritten during the mutation, must still be
        reported as postcondition_tracked_changes_detected (false negative fix --
        the previous simple set-diff treated any pre-existing `git status` line
        as unconditionally safe, regardless of whether its content actually
        changed during this command's own mutation)."""
        preexisting = tmp_project / "preexisting_overwritten.md"
        preexisting.write_text("original content")
        write_root = "artifacts/1166/"
        pre_snapshot, pre_err = _exec._capture_pre_mutation_snapshot(tmp_project, 1166, write_root)
        assert pre_err is None
        assert "preexisting_overwritten.md" in pre_snapshot.candidate_paths

        # Simulate the mutation overwriting the pre-existing file's content
        # in place (different size -- and, on most filesystems, different
        # mtime_ns -- from the pre-mutation snapshot).
        preexisting.write_text("overwritten content with a very different length")

        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, pre_snapshot)
        assert any("preexisting_overwritten.md" in v for v in violations)

    def test_postcondition_detects_new_untracked_or_tracked_change(self, tmp_project):
        """AC4 (non-regression): a new untracked file created during the
        mutation (outside allowed_prefix) is still detected as a violation
        under the new metadata-snapshot comparison, matching the previous
        fail-closed behavior for genuinely new changes."""
        write_root = "artifacts/1166/"
        pre_snapshot, pre_err = _exec._capture_pre_mutation_snapshot(tmp_project, 1166, write_root)
        assert pre_err is None
        assert pre_snapshot.candidate_paths == frozenset()

        (tmp_project / "new_during_mutation.md").write_text("created during the mutation")

        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, pre_snapshot)
        assert any("new_during_mutation.md" in v for v in violations)


# =============================================================================
# Issue #2163 review fix_delta (PR #2178 REQUEST_CHANGES): real Git-repository
# regression tests for the porcelain=v2 -z / union-transition / metadata-
# identity / fail-closed-error primitives. These run real `git status
# --porcelain=v2 -z` subprocess calls against a real repository (not mocked)
# so the tests exercise the same code path production traffic uses.
# =============================================================================


class TestPostconditionRealGitRegressions:
    """OWNER-specified minimum regression coverage (PR #2178 review comment)."""

    def _snapshot(self, project_root, write_root="artifacts/1166/"):
        state, err = _exec._capture_pre_mutation_snapshot(project_root, 1166, write_root)
        assert err is None, f"unexpected capture error: {err}"
        return state

    def test_rejects_new_untracked_path_with_space(self, tmp_project):
        """P0-1: a brand-new untracked path containing a space must be parsed
        correctly (porcelain=v1 C-style-quotes it; porcelain=v2 -z does not)
        and detected as a violation, not silently dropped by a quote-vs-lstat
        mismatch."""
        before = self._snapshot(tmp_project)
        (tmp_project / "space name.txt").write_text("new file with a space in its name")
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, "artifacts/1166/", before)
        assert any("space name.txt" in v for v in violations)

    def test_rejects_path_with_tab_newline_quote_and_backslash(self, tmp_project):
        """P0-1: pathnames with characters porcelain=v1 would C-style-quote
        (tab, double quote, backslash) must still be individually authorized
        under -z's literal-bytes encoding."""
        before = self._snapshot(tmp_project)
        weird_name = 'weird"quote\\slash_and_tabbed.txt'
        (tmp_project / weird_name).write_text("weird pathname content")
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, "artifacts/1166/", before)
        assert any(weird_name in v for v in violations)

    def test_rejects_rename_from_allowed_root_to_forbidden_root(self, tmp_project):
        """P0-1: a rename whose destination lands outside allowed_prefix must
        be rejected even though the whole porcelain line would have started
        with the allowed prefix under the old whole-line-prefix check."""
        write_root = "artifacts/1166/"
        allowed_dir = tmp_project / "artifacts" / "1166"
        allowed_dir.mkdir(parents=True, exist_ok=True)
        src = allowed_dir / "inside.txt"
        src.write_text("moved out of the allowed root")
        subprocess.run(
            ["git", "-C", str(tmp_project), "add", "-A"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_project), "commit", "-m", "seed"],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        before = self._snapshot(tmp_project, write_root)
        dest = tmp_project / "outside.txt"
        subprocess.run(
            ["git", "-C", str(tmp_project), "mv", str(src), str(dest)],
            capture_output=True,
            check=True,
        )
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("outside.txt" in v for v in violations)

    def test_rejects_rename_from_forbidden_root_to_allowed_root(self, tmp_project):
        """P0-1: a rename whose SOURCE is outside allowed_prefix must be
        rejected even though its destination lands inside allowed_prefix."""
        write_root = "artifacts/1166/"
        src = tmp_project / "forbidden_source.txt"
        src.write_text("about to be moved into the allowed root")
        subprocess.run(
            ["git", "-C", str(tmp_project), "add", "-A"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_project), "commit", "-m", "seed"],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        before = self._snapshot(tmp_project, write_root)
        (tmp_project / "artifacts" / "1166").mkdir(parents=True, exist_ok=True)
        dest = tmp_project / "artifacts" / "1166" / "moved_in.txt"
        subprocess.run(
            ["git", "-C", str(tmp_project), "mv", str(src), str(dest)],
            capture_output=True,
            check=True,
        )
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("moved_in.txt" in v or "forbidden_source.txt" in v for v in violations)

    def test_rejects_newly_deleted_tracked_file(self, tmp_project):
        """P0-2: a clean tracked file deleted during the mutation window must
        be rejected even though it never appeared as a `git status` candidate
        before the deletion (it was clean)."""
        write_root = "artifacts/1166/"
        tracked = tmp_project / "README_TRACKED.md"
        tracked.write_text("tracked and clean")
        subprocess.run(
            ["git", "-C", str(tmp_project), "add", "-A"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_project), "commit", "-m", "seed"],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        before = self._snapshot(tmp_project, write_root)
        assert "README_TRACKED.md" not in before.candidate_paths  # clean -> not a candidate yet
        tracked.unlink()
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("README_TRACKED.md" in v for v in violations)

    def test_rejects_deleted_preexisting_untracked_file(self, tmp_project):
        """P0-2: a pre-existing untracked file deleted during the mutation
        window must be rejected even though its deletion leaves ZERO trace in
        post-mutation `git status` output (an untracked path that no longer
        exists is simply absent from the listing)."""
        write_root = "artifacts/1166/"
        preexisting = tmp_project / "preexisting_untracked_to_delete.md"
        preexisting.write_text("will be deleted")
        before = self._snapshot(tmp_project, write_root)
        assert "preexisting_untracked_to_delete.md" in before.candidate_paths
        preexisting.unlink()
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("preexisting_untracked_to_delete.md" in v for v in violations)

    def test_rejects_untracked_to_staged_transition(self, tmp_project):
        """P0-3: `git add` on a previously-untracked file (staging it) must be
        rejected even when filesystem metadata (mtime/size/content) is
        completely unchanged -- the git INDEX state itself changed."""
        write_root = "artifacts/1166/"
        target = tmp_project / "untracked_then_staged.md"
        target.write_text("unchanged content throughout")
        before = self._snapshot(tmp_project, write_root)
        subprocess.run(
            ["git", "-C", str(tmp_project), "add", str(target)], capture_output=True, check=True
        )
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("untracked_then_staged.md" in v for v in violations)

    def test_rejects_unstaged_to_staged_transition(self, tmp_project):
        """P0-3: `git add` on a previously-unstaged (already-tracked, already
        modified) file must be rejected -- the XY code moves from ` M` to
        `M ` even though the working-tree bytes never change again."""
        write_root = "artifacts/1166/"
        tracked = tmp_project / "tracked_modified.md"
        tracked.write_text("original tracked content")
        subprocess.run(
            ["git", "-C", str(tmp_project), "add", "-A"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_project), "commit", "-m", "seed"],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        tracked.write_text("modified but not yet staged")
        before = self._snapshot(tmp_project, write_root)
        assert "tracked_modified.md" in before.candidate_paths
        subprocess.run(
            ["git", "-C", str(tmp_project), "add", str(tracked)], capture_output=True, check=True
        )
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("tracked_modified.md" in v for v in violations)

    def test_rejects_same_size_content_change_with_restored_mtime(self, tmp_project):
        """P1-1: a same-size content rewrite with the mtime restored to its
        original value must still be rejected via the SHA-256 content digest
        comparison (mtime/size alone cannot distinguish this)."""
        write_root = "artifacts/1166/"
        target = tmp_project / "same_size_rewrite.md"
        target.write_text("AAAAAAAAAA")
        before = self._snapshot(tmp_project, write_root)
        original_stat = target.stat()
        target.write_text("BBBBBBBBBB")
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("same_size_rewrite.md" in v for v in violations)

    def test_rejects_chmod_only_change(self, tmp_project):
        """P1-1: a mode-only change (chmod +x) on a pre-existing untracked
        file, with content/mtime/size unchanged, must still be rejected via
        `st_mode` comparison."""
        write_root = "artifacts/1166/"
        target = tmp_project / "chmod_only.md"
        target.write_text("content never changes")
        before = self._snapshot(tmp_project, write_root)
        target.chmod(0o755)
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("chmod_only.md" in v for v in violations)

    def test_rejects_regular_to_symlink_fifo_or_socket_transition(self, tmp_project):
        """P1-1: replacing a regular file with a symlink (same relative path)
        must be rejected via `node_type` comparison."""
        write_root = "artifacts/1166/"
        target = tmp_project / "type_swap.md"
        target.write_text("a regular file")
        before = self._snapshot(tmp_project, write_root)
        target.unlink()
        target.symlink_to(tmp_project / "type_swap_target_does_not_matter.md")
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, write_root, before)
        assert any("type_swap.md" in v for v in violations)

    def test_fails_closed_on_lstat_eacces_eio_enotdir(self, tmp_project, monkeypatch):
        """P1-1: any `OSError` other than `FileNotFoundError` while stat-ing a
        candidate path (e.g. EACCES/EIO/ENOTDIR) must raise/propagate as a
        capture failure, not be silently folded into "absent"."""
        write_root = "artifacts/1166/"
        target = tmp_project / "unreadable.md"
        target.write_text("content")

        real_lstat = Path.lstat

        def _flaky_lstat(self, *a, **kw):
            if self.name == "unreadable.md":
                raise PermissionError(13, "Permission denied")
            return real_lstat(self, *a, **kw)

        monkeypatch.setattr(Path, "lstat", _flaky_lstat)
        state, err = _exec._capture_pre_mutation_snapshot(tmp_project, 1166, write_root)
        assert state is None
        assert err is not None
        assert "lstat_failed" in err

    def test_fails_before_remote_mutation_when_pre_snapshot_fails(self, tmp_project, monkeypatch):
        """P1-1: `_capture_pre_mutation_snapshot`'s error must be surfaced
        distinctly from a successful-but-empty snapshot (the previous
        `except Exception: return {}` fallback was indistinguishable from
        "nothing to compare against"), giving callers what they need to stop
        before attempting any remote mutation."""
        write_root = "artifacts/1166/"

        def _boom(*a, **kw):
            raise _exec._RepoStateCaptureError(
                "git_status_v2_exception: simulated transport failure"
            )

        monkeypatch.setattr(_exec, "_collect_repo_state", _boom)
        state, err = _exec._capture_pre_mutation_snapshot(tmp_project, 1166, write_root)
        assert state is None
        assert err is not None and "simulated transport failure" in err

    def test_issue_body_retry_repairs_marker_after_remote_applied(self, tmp_project, monkeypatch):
        """P1-3: on a retry after a prior attempt's remote PATCH succeeded but
        local postcondition/marker writing failed, a fresh remote readback
        that already matches the desired body must repair the marker and
        report `already_applied` -- not fall through to a stale-precondition
        rejection because no marker exists locally yet (the old code path
        only checked remote-vs-desired equality inside the
        `if marker_data is not None` branch)."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        new_body = "updated body content"
        new_body_sha256 = "sha256:" + hashlib.sha256(new_body.encode("utf-8")).hexdigest()

        marker_path = _exec._issue_metadata_marker_path(
            tmp_project, 1166, COMMAND_ID_ISSUE_BODY_UPDATE, "issue_body_update.marker.json"
        )
        assert not marker_path.exists()  # no local marker survived the prior attempt's failure

        # Remote already reflects the desired state -- as if a prior PATCH
        # succeeded but the caller crashed / failed locally before writing
        # the marker.
        monkeypatch.setattr(
            _exec, "_fetch_issue_body_and_updated_at", lambda *a, **k: (new_body, "t2", "")
        )

        patch_calls = {"n": 0}

        def _fail_if_patched(*a, **k):
            patch_calls["n"] += 1
            return ""  # would only be reached if the bug regresses and a second PATCH is attempted

        monkeypatch.setattr(_exec, "_patch_issue_body", _fail_if_patched)

        args = SimpleNamespace(
            issue_number=1166,
            command_id=COMMAND_ID_ISSUE_BODY_UPDATE,
            repo=TRUSTED_REPO,
            dry_run=False,
            output_json=False,
        )
        # previous_body_sha256/previous_updated_at intentionally do NOT match
        # current remote state (they describe the state BEFORE the prior
        # successful-but-unmarked PATCH) -- the stale-precondition check must
        # never even be reached because the already-applied short-circuit
        # above it takes priority.
        input_data = {
            "previous_body_sha256": "sha256:" + hashlib.sha256(b"stale original body").hexdigest(),
            "previous_updated_at": "t0",
            "new_body": new_body,
            "new_body_sha256": new_body_sha256,
        }
        _fail, _ok, calls = _capture_fail_ok()

        rc = _exec._run_issue_body_update(args, input_data, "gh", _fail, _ok)

        assert rc == 0
        assert patch_calls["n"] == 0  # no duplicate remote PATCH was attempted
        assert calls["ok_extra"]["status_detail"] == "already_applied"
        assert calls["ok_extra"]["marker_state"] == "already_applied_marker_repaired"
        assert calls["ok_extra"]["idempotency_marker_repaired"] is True
        assert marker_path.exists()  # the marker was repaired for future idempotency
        repaired = json.loads(marker_path.read_text())
        assert repaired["new_body_sha256"] == new_body_sha256


# =============================================================================
# Issue #2163 AC5: issue_body.update / issue_comment.publish postcondition
# failure must report mutation_outcome so callers can distinguish "remote
# mutation succeeded but local postcondition failed" from a mutation that was
# never attempted (precondition reject). Design reference:
# `issue_content.update`'s existing `_finalize_remote_success` closure.
# =============================================================================


def _capture_fail_ok():
    calls: dict = {}

    def _fail(reason, errors=None, status="error", extra=None):
        calls["reason"] = reason
        calls["errors"] = errors
        calls["status"] = status
        calls["extra"] = extra
        return 1

    def _ok(extra):
        calls["ok_extra"] = extra
        return 0

    return _fail, _ok, calls


class TestMutationOutcomeOnPostconditionFailure:
    def test_issue_body_update_postcondition_failure_reports_mutation_outcome(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        old_body = "old body"
        new_body = "new body"
        old_body_sha256 = "sha256:" + hashlib.sha256(old_body.encode("utf-8")).hexdigest()
        new_body_sha256 = "sha256:" + hashlib.sha256(new_body.encode("utf-8")).hexdigest()

        readback_calls = {"count": 0}

        def fake_fetch(issue_number, repo, gh_bin):
            readback_calls["count"] += 1
            if readback_calls["count"] == 1:
                return old_body, "t1", ""
            return new_body, "t2", ""

        monkeypatch.setattr(_exec, "_fetch_issue_body_and_updated_at", fake_fetch)
        monkeypatch.setattr(_exec, "_patch_issue_body", lambda *a, **k: "")
        monkeypatch.setattr(
            _exec, "_check_no_tracked_changes", lambda *a, **k: ["??:unexpected_leftover.txt"]
        )

        args = SimpleNamespace(
            issue_number=1166,
            command_id=COMMAND_ID_ISSUE_BODY_UPDATE,
            repo=TRUSTED_REPO,
            dry_run=False,
            output_json=False,
        )
        input_data = {
            "previous_body_sha256": old_body_sha256,
            "previous_updated_at": "t1",
            "new_body": new_body,
            "new_body_sha256": new_body_sha256,
        }
        _fail, _ok, calls = _capture_fail_ok()

        rc = _exec._run_issue_body_update(args, input_data, "gh", _fail, _ok)

        assert rc == 1
        assert calls["reason"] == "postcondition_tracked_changes_detected"
        assert calls["status"] == "applied_but_local_postcondition_failed"
        assert calls["extra"]["mutation_outcome"] == "applied"
        assert calls["extra"]["remote_receipt"]["body_sha256"] == new_body_sha256
        assert calls["extra"]["remote_receipt"]["observed_updated_at"] == "t2"
        assert "retry_policy" in calls["extra"]

    def test_issue_comment_publish_postcondition_failure_reports_mutation_outcome(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        comment_body = "status update <!-- marker-1166 -->"
        marker = "<!-- marker-1166 -->"
        expected_body_sha256 = hashlib.sha256(comment_body.encode()).hexdigest()

        monkeypatch.setattr(_exec, "_find_marker_matches", lambda *a, **k: ([], ""))
        monkeypatch.setattr(
            _exec, "_post_gh_comment", lambda *a, **k: ("https://example/comment/1", "1", "")
        )
        monkeypatch.setattr(
            _exec,
            "_readback_by_marker_literal",
            lambda *a, **k: {
                "comment_id": "1",
                "comment_url": "https://example/comment/1",
                "body_sha256": expected_body_sha256,
            },
        )
        monkeypatch.setattr(
            _exec, "_check_no_tracked_changes", lambda *a, **k: ["??:unexpected_leftover.txt"]
        )

        args = SimpleNamespace(
            issue_number=1166,
            command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            repo=TRUSTED_REPO,
            dry_run=False,
            output_json=False,
        )
        input_data = {"comment_body": comment_body, "marker": marker}
        _fail, _ok, calls = _capture_fail_ok()

        rc = _exec._run_issue_comment_publish(args, "", input_data, "gh", _fail, _ok)

        assert rc == 1
        assert calls["reason"] == "postcondition_tracked_changes_detected"
        assert calls["status"] == "applied_but_local_postcondition_failed"
        assert calls["extra"]["mutation_outcome"] == "applied"
        assert calls["extra"]["remote_receipt"]["comment_id"] == "1"
        assert calls["extra"]["remote_receipt"]["body_sha256"] == expected_body_sha256
        assert "retry_policy" in calls["extra"]


# =============================================================================
# Issue #1116 AC4: issue_comment.publish marker-match + digest-mismatch update
# branch (`_run_issue_comment_publish` PATCH path, author-checked).
# =============================================================================


def test_issue_comment_publish_update_on_marker_match_digest_mismatch(tmp_project, monkeypatch):
    """Marker (identity) matches an existing remote comment, but its body
    digest differs from the caller's new comment_body, and the existing
    comment's author is the currently authenticated actor: the executor
    must PATCH the existing comment (not fail closed, not double-post)."""
    monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
    marker = "<!-- issue_finalize_report:v1 repo=o/r issue=1166 run_id=abc -->"
    new_comment_body = (
        f"{marker}\n<!-- issue_finalize_report_digest:v1 sha256=" + "b" * 64 + " -->\n\nnew payload"
    )
    old_comment_body = (
        f"{marker}\n<!-- issue_finalize_report_digest:v1 sha256=" + "a" * 64 + " -->\n\nold payload"
    )
    expected_body_sha256 = hashlib.sha256(new_comment_body.encode()).hexdigest()
    remote_comment = {
        "id": "IC_kwDOSfQcDc8AAAAB0000",
        "url": "https://github.com/o/r/issues/1166#issuecomment-555000",
        "body": old_comment_body,
        "author": {"login": "loop-protocol-bot"},
    }

    monkeypatch.setattr(_exec, "_find_marker_matches", lambda *a, **k: ([remote_comment], ""))
    monkeypatch.setattr(
        _exec, "_fetch_authenticated_login", lambda *a, **k: ("loop-protocol-bot", "")
    )

    patch_calls: list[tuple] = []

    def _fake_patch(numeric_comment_id, repo, body, gh_bin):
        patch_calls.append((numeric_comment_id, repo, body, gh_bin))
        return ""

    monkeypatch.setattr(_exec, "_patch_gh_comment", _fake_patch)
    monkeypatch.setattr(
        _exec,
        "_readback_by_marker_literal",
        lambda *a, **k: {
            "comment_id": "IC_kwDOSfQcDc8AAAAB0000",
            "comment_url": remote_comment["url"],
            "body_sha256": expected_body_sha256,
        },
    )
    monkeypatch.setattr(_exec, "_check_no_tracked_changes", lambda *a, **k: [])
    post_calls: list = []
    monkeypatch.setattr(
        _exec,
        "_post_gh_comment",
        lambda *a, **k: post_calls.append(a) or ("", "", "should_not_post"),
    )

    args = SimpleNamespace(
        issue_number=1166,
        command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        repo=TRUSTED_REPO,
        dry_run=False,
        output_json=False,
    )
    input_data = {"comment_body": new_comment_body, "marker": marker}
    _fail, _ok, calls = _capture_fail_ok()

    rc = _exec._run_issue_comment_publish(args, "", input_data, "gh", _fail, _ok)

    assert rc == 0
    assert post_calls == []  # create-path POST must never be attempted here
    assert len(patch_calls) == 1
    assert patch_calls[0][0] == "555000"  # numeric id extracted from the comment permalink
    assert patch_calls[0][2] == new_comment_body
    assert calls["ok_extra"]["status_detail"] == "updated"
    assert calls["ok_extra"]["body_sha256"] == expected_body_sha256
    assert calls["ok_extra"]["idempotency_marker_written"] is True


def test_issue_comment_publish_update_rejects_author_mismatch(tmp_project, monkeypatch):
    """Marker matches but the existing comment's author differs from the
    currently authenticated actor: this must fail closed as an identity
    conflict, and PATCH must never be attempted."""
    monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
    marker = "<!-- issue_finalize_report:v1 repo=o/r issue=1166 run_id=abc -->"
    new_comment_body = (
        f"{marker}\n<!-- issue_finalize_report_digest:v1 sha256=" + "b" * 64 + " -->\n\nnew payload"
    )
    old_comment_body = (
        f"{marker}\n<!-- issue_finalize_report_digest:v1 sha256=" + "a" * 64 + " -->\n\nold payload"
    )
    remote_comment = {
        "id": "IC_kwDOSfQcDc8AAAAB0001",
        "url": "https://github.com/o/r/issues/1166#issuecomment-555001",
        "body": old_comment_body,
        "author": {"login": "someone-else"},
    }

    monkeypatch.setattr(_exec, "_find_marker_matches", lambda *a, **k: ([remote_comment], ""))
    monkeypatch.setattr(
        _exec, "_fetch_authenticated_login", lambda *a, **k: ("loop-protocol-bot", "")
    )

    patch_calls: list = []
    monkeypatch.setattr(_exec, "_patch_gh_comment", lambda *a, **k: patch_calls.append(a) or "")

    args = SimpleNamespace(
        issue_number=1166,
        command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        repo=TRUSTED_REPO,
        dry_run=False,
        output_json=False,
    )
    input_data = {"comment_body": new_comment_body, "marker": marker}
    _fail, _ok, calls = _capture_fail_ok()

    rc = _exec._run_issue_comment_publish(args, "", input_data, "gh", _fail, _ok)

    assert rc == 1
    assert calls["reason"] == "remote_marker_author_mismatch_pre_mutation"
    assert patch_calls == []


# =============================================================================
# Issue #1632: issue_dependency.remove
# =============================================================================

ISSUE_DEPENDENCY_REMOVE_COMMAND_ID = "issue_dependency.remove"
ISSUE_DEPENDENCY_REMOVE_SCHEMA = "ISSUE_DEPENDENCY_REMOVE_INPUT_V1"


def _dep_remove_input_dir(tmp_project, issue_number=1523):
    d = (
        tmp_project
        / "artifacts"
        / str(issue_number)
        / "issue-metadata"
        / ISSUE_DEPENDENCY_REMOVE_COMMAND_ID
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dep_remove_write_input(tmp_project, issue_number=1523, **overrides):
    d = _dep_remove_input_dir(tmp_project, issue_number)
    payload = {
        "schema": ISSUE_DEPENDENCY_REMOVE_SCHEMA,
        "issue_number": issue_number,
        "repo": TRUSTED_REPO,
        "target_blocker_number": 1403,
        "expected_blocked_issue_node_id": "ISSUE_NODE_BLOCKED",
        "expected_blocker_node_id": "ISSUE_NODE_BLOCKER",
        "expected_blocked_by_numbers": [1403],
        "expected_pre_mutation_snapshot_sha256": "sha256:" + "0" * 64,
        "idempotency_key": f"{TRUSTED_REPO}:{issue_number}:1403:v1",
    }
    payload.update(overrides)
    f = d / "input.json"
    f.write_text(json.dumps(payload))
    return (
        f"artifacts/{issue_number}/issue-metadata/{ISSUE_DEPENDENCY_REMOVE_COMMAND_ID}/input.json"
    )


def _dep_remove_main_args(tmp_project, input_rel_path, issue_number=1523):
    return [
        "--command-id",
        ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
        "--issue-number",
        str(issue_number),
        "--input-file",
        input_rel_path,
        "--repo",
        TRUSTED_REPO,
    ]


def _blocked_by_page(
    blocked_id, blocked_number, nodes, has_next=False, end_cursor=None, state="OPEN"
):
    return {
        "repository": {
            "issue": {
                "id": blocked_id,
                "number": blocked_number,
                "state": state,
                "blockedBy": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                    "nodes": nodes,
                },
            }
        }
    }


def _node(node_id, number, state="CLOSED"):
    return {
        "id": node_id,
        "number": number,
        "state": state,
        "repository": {"nameWithOwner": TRUSTED_REPO},
    }


class TestIssueDependencyRemoveAllPageReadback:
    """AC2: exhaustive all-page readback; cursor/schema drift is rejected."""

    def test_issue_dependency_remove_reads_all_pages_and_rejects_cursor_or_schema_drift(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        _dep_remove_write_input(
            tmp_project,
            expected_blocked_by_numbers=[1400, 1403],
            expected_pre_mutation_snapshot_sha256="sha256:" + "0" * 64,
        )

        page1 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("N1400", 1400)],
            has_next=True,
            end_cursor="CURSOR1",
        )
        page2 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("N1403", 1403)],
            has_next=False,
        )

        with patch.object(
            _exec,
            "_graphql_call",
            side_effect=[
                (page1, ""),
                (page2, ""),
            ],
        ):
            result, err = _exec._fetch_blocked_by_all_pages(1523, TRUSTED_REPO, "gh", {})
        assert err == ""
        assert result["page_count"] == 2
        assert sorted(n["number"] for n in result["nodes"]) == [1400, 1403]

        # Cursor/schema drift: hasNextPage True but endCursor missing/None.
        bad_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("N1400", 1400)],
            has_next=True,
            end_cursor=None,
        )
        with patch.object(_exec, "_graphql_call", return_value=(bad_page, "")):
            result2, err2 = _exec._fetch_blocked_by_all_pages(1523, TRUSTED_REPO, "gh", {})
        assert result2 is None
        assert "cursor" in err2

        # Duplicate node across pages is rejected.
        dup_page1 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("N1403", 1403)],
            has_next=True,
            end_cursor="CURSOR1",
        )
        dup_page2 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("N1403", 1403)],
            has_next=False,
        )
        with patch.object(
            _exec,
            "_graphql_call",
            side_effect=[
                (dup_page1, ""),
                (dup_page2, ""),
            ],
        ):
            result3, err3 = _exec._fetch_blocked_by_all_pages(1523, TRUSTED_REPO, "gh", {})
        assert result3 is None
        assert "duplicate" in err3


class TestIssueDependencyRemoveCredentialActor:
    """AC3: trusted credential actor readback gates the mutation."""

    def test_issue_dependency_remove_rejects_untrusted_or_unreadable_credential_actor(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        input_rel = _dep_remove_write_input(tmp_project)

        # Unreadable actor (login fetch fails) -- fail closed, no GraphQL call.
        with patch.object(  # noqa: SIM117
            _exec,
            "_fetch_authenticated_login",
            return_value=(None, "gh_api_authenticated_user_empty"),
        ):
            with patch.object(_exec, "_graphql_call") as mock_gql:
                rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc == 1
        mock_gql.assert_not_called()

        # Authorized-but-insufficient permission -- fail closed, no GraphQL call.
        with patch.object(_exec, "_fetch_authenticated_login", return_value=("bot", "")):  # noqa: SIM117
            with patch.object(
                _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "read", "")
            ):
                with patch.object(_exec, "_graphql_call") as mock_gql2:
                    rc2 = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc2 == 1
        mock_gql2.assert_not_called()


class TestIssueDependencyRemoveClosedStatusNoRetry:
    """AC4: closed result status set; transport/GraphQL failure is never
    automatically retried within one invocation."""

    def test_issue_dependency_remove_records_closed_status_and_never_retries_mutation(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        input_rel = _dep_remove_write_input(tmp_project)

        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        pre_hash = _exec._compute_blocked_by_snapshot_sha256(
            "ISSUE_NODE_BLOCKED",
            1523,
            [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}],
        )
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )

        call_count = {"mutation": 0}

        def fake_graphql(gh_bin, env, query, variables):
            if "removeBlockedBy" in query:
                call_count["mutation"] += 1
                return None, "gh_api_graphql_failed: transport error"
            return pre_page, ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        assert call_count["mutation"] == 1  # never retried automatically
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "transport_or_schema_error"

    def test_issue_dependency_remove_success_reports_removed_status(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        post_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])

        mutation_response = {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
            }
        }
        responses = iter([pre_page, mutation_response, post_page])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "admin", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"
        assert out["idempotency_marker_written"] is True


class TestIssueDependencyRemovePostconditionAndIdempotency:
    """AC5: all-page post-mutation readback + idempotency marker."""

    def test_issue_dependency_remove_requires_all_page_post_snapshot_and_idempotency_marker(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        # Postcondition failure: target relationship still present after mutation.
        post_page_still_present = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        responses = iter(
            [
                pre_page,
                {
                    "removeBlockedBy": {
                        "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                        "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                        "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
                    }
                },
                post_page_still_present,
            ]
        )

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "postcondition_rejected"

        # Idempotency (Issue #1667 review fix_delta P2): a FULLY valid marker
        # present + fresh readback confirms already absent -> already_completed.
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project,
            1523,
            ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
            "issue_dependency_remove.marker.json",
        )
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_DEPENDENCY_REMOVE_MARKER_V1",
                    "issue_number": 1523,
                    "repo": TRUSTED_REPO,
                    "target_blocker_number": 1403,
                    "blocked_issue_id": "ISSUE_NODE_BLOCKED",
                    "blocked_issue_number": 1523,
                    "blocker_node_id": "ISSUE_NODE_BLOCKER",
                    "idempotency_key": f"{TRUSTED_REPO}:1523:1403:v1",
                    "actor_login": "bot",
                    "actor_permission": "write",
                    "status_detail": "removed",
                }
            )
        )
        already_absent_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])
        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(
                _exec, "_graphql_call", return_value=(already_absent_page, "")
            ) as mock_gql:
                rc2 = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc2 == 0
        out2 = json.loads(capsys.readouterr().out)
        assert out2["status"] == "already_completed"
        # Only the pre-mutation readback call was made -- no mutation attempted.
        assert mock_gql.call_count == 1

        # Issue #1667 review fix_delta P2: an INCOMPLETE marker (missing the
        # closed-schema fields, e.g. only idempotency_key) is never trusted
        # as already_completed, even though the target is remotely absent --
        # this is routed to human judgment (postcondition_rejected) instead.
        marker_path.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_DEPENDENCY_REMOVE_MARKER_V1",
                    "idempotency_key": f"{TRUSTED_REPO}:1523:1403:v1",
                }
            )
        )
        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(
                _exec, "_graphql_call", return_value=(already_absent_page, "")
            ) as mock_gql3:
                rc3 = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc3 == 1
        out3 = json.loads(capsys.readouterr().out)
        assert out3["status"] == "postcondition_rejected"
        assert "already_completed_marker_invalid" in out3["reason"]
        # Still no mutation attempted -- the ambiguity is resolved without a
        # network write.
        assert mock_gql3.call_count == 1


class TestIssueDependencyRemoveFailurePathsFailClosed:
    """AC6: dedicated failure-path fail-closed matrix, no real network mutation."""

    def test_issue_dependency_remove_failure_paths_fail_closed_without_network_mutation(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)

        def _assert_blocked(overrides, expect_rc=2):
            input_rel = _dep_remove_write_input(tmp_project, **overrides)
            with patch.object(_exec, "_graphql_call") as mock_gql:
                rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
            assert rc == expect_rc, f"overrides={overrides} rc={rc}"
            mock_gql.assert_not_called()

        # unknown key
        _assert_blocked({"unexpected_field": "x"})
        # bool number
        _assert_blocked({"target_blocker_number": True})
        # duplicate set
        _assert_blocked({"expected_blocked_by_numbers": [1403, 1403]})
        # unsorted set
        _assert_blocked({"expected_blocked_by_numbers": [1403, 100]})
        # oversize set
        oversize = list(range(1, _exec.ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS + 2))
        _assert_blocked(
            {"expected_blocked_by_numbers": oversize, "target_blocker_number": oversize[0]}
        )
        # null issue_number
        _assert_blocked({"issue_number": None})
        # wrong repo
        _assert_blocked({"repo": "attacker/evil-repo"})

        # -- GraphQL errors / cursor failure / schema drift during precondition
        # readback -- fail closed before any mutation call.
        input_rel = _dep_remove_write_input(tmp_project)
        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(
                _exec, "_graphql_call", return_value=(None, "gh_api_graphql_errors: boom")
            ) as mock_gql:
                rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc == 1
        mock_gql.assert_called_once()

        # -- Hash / actor / node-id / state mismatch: precondition_rejected,
        # no mutation call.
        wrong_state_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403, state="OPEN")],
        )
        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", return_value=(wrong_state_page, "")):
                rc2 = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc2 == 1

        # -- Pre/post TOCTOU: mutation succeeds but a concurrent change means
        # the non-target set differs post-mutation -- postcondition_rejected.
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel2 = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        toctou_post_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("N_NEW", 9999)],
        )
        responses = iter(
            [
                pre_page,
                {
                    "removeBlockedBy": {
                        "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                        "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                        "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
                    }
                },
                toctou_post_page,
            ]
        )

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc3 = _exec.main(_dep_remove_main_args(tmp_project, input_rel2) + ["--json"])
        assert rc3 == 1

        # -- Second mutation attempt within one invocation is never issued:
        # verified structurally by the no-retry test above
        # (call_count["mutation"] == 1). Here we additionally assert that a
        # mutation transport error does not trigger any subsequent GraphQL
        # call at all (fully fail-closed, zero further network activity).
        call_log = []

        def fake_graphql_single_call(gh_bin, env, query, variables):
            call_log.append(query)
            if "removeBlockedBy" in query:
                return None, "gh_api_graphql_errors: boom"
            return pre_page, ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql_single_call):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc4 = _exec.main(_dep_remove_main_args(tmp_project, input_rel2) + ["--json"])
        assert rc4 == 1
        assert sum(1 for q in call_log if "removeBlockedBy" in q) == 1


# =============================================================================
# Issue #1667 review fix_delta P0: GraphQL field-name exactness
# =============================================================================


class TestIssueDependencyRemoveMutationFieldNames:
    """P0: the GitHub GraphQL schema names the RemoveBlockedByInput field
    `blockingIssueId` -- NOT `blockedByIssueId` (that name never existed on
    the input type). This is a read-only, static string check on the fixed
    mutation document; no network call is made."""

    def test_mutation_uses_blocking_issue_id_not_blocked_by_issue_id(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "blockingIssueId" in mutation
        assert "blockedByIssueId" not in mutation

    def test_mutation_declares_client_mutation_id_variable(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "$clientMutationId: String" in mutation
        assert "clientMutationId: $clientMutationId" in mutation

    def test_mutation_response_selects_blocking_issue_and_client_mutation_id(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "blockingIssue { id number }" in mutation
        assert "clientMutationId" in mutation.split("removeBlockedBy", 1)[1]

    def test_mutation_call_site_variables_use_official_field_name(self, tmp_project, monkeypatch):
        """Static/behavioral check: the actual _graphql_call invocation for
        the mutation is made with the exact variable keys
        {issueId, blockingIssueId, clientMutationId} -- never
        blockedByIssueId."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        captured_variables = {}

        def fake_graphql(gh_bin, env, query, variables):
            if "removeBlockedBy" in query:
                captured_variables.update(variables)
                return None, "gh_api_graphql_errors: stop_before_mutation_succeeds"
            return pre_page, ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert set(captured_variables.keys()) == {"issueId", "blockingIssueId", "clientMutationId"}
        assert "blockedByIssueId" not in captured_variables


# =============================================================================
# Issue #1667 review fix_delta P0: RemoveBlockedByInput schema compatibility
# (read-only, static -- no network mutation)
# =============================================================================


class TestRemoveBlockedByInputSchemaCompatibility:
    """P0: static compatibility check against the official GitHub GraphQL
    schema's RemoveBlockedByInput type:

        input RemoveBlockedByInput {
          blockingIssueId: ID!
          clientMutationId: String
          issueId: ID!
        }

    This test never makes a network call -- it only inspects the fixed
    mutation document string this executor sends.
    """

    _OFFICIAL_INPUT_FIELDS = frozenset({"issueId", "blockingIssueId", "clientMutationId"})

    def test_mutation_input_object_fields_match_official_schema(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        # Extract the input object body between "input: {" and the matching "}"
        start = mutation.index("input: {") + len("input: {")
        end = mutation.index("}", start)
        input_body = mutation[start:end]
        # Field names appear as "<name>: $<name>" pairs.
        declared_fields = {
            part.split(":", 1)[0].strip() for part in input_body.split(",") if part.strip()
        }
        assert declared_fields == self._OFFICIAL_INPUT_FIELDS

    def test_mutation_variable_declarations_match_official_schema(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        header = mutation.split(")", 1)[0]
        # Variable declarations look like "$name: Type"
        declared_vars = {
            piece.split(":", 1)[0].strip().lstrip("$")
            for piece in header.split("(", 1)[1].split(",")
            if piece.strip()
        }
        assert declared_vars == self._OFFICIAL_INPUT_FIELDS

    def test_issue_id_and_blocking_issue_id_are_non_null_id_type(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "$issueId: ID!" in mutation
        assert "$blockingIssueId: ID!" in mutation

    def test_client_mutation_id_is_nullable_string_type(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "$clientMutationId: String" in mutation
        assert "$clientMutationId: String!" not in mutation


# =============================================================================
# Issue #1667 review fix_delta P1: removeBlockedBy response validation
# =============================================================================


class TestValidateRemoveBlockedByMutationResponse:
    """P1: the mutation response (RemoveBlockedByPayload) must be validated
    before the executor trusts that the mutation succeeded against the
    intended target."""

    _KW = dict(  # noqa: C408, RUF012
        expected_blocked_issue_node_id="ISSUE_NODE_BLOCKED",
        expected_blocked_issue_number=1523,
        expected_blocker_node_id="ISSUE_NODE_BLOCKER",
        expected_blocker_number=1403,
        expected_client_mutation_id=f"{TRUSTED_REPO}:1523:1403:v1",
    )

    def _valid_response(self):
        return {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
            }
        }

    def test_valid_response_passes(self):
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            self._valid_response(), **self._KW
        )
        assert err == ""
        assert is_schema_error is False

    def test_not_a_dict_is_schema_error(self):
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(None, **self._KW)
        assert err != ""
        assert is_schema_error is True

    def test_missing_remove_blocked_by_key_is_schema_error(self):
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response({}, **self._KW)
        assert "missing_remove_blocked_by_payload" in err
        assert is_schema_error is True

    def test_missing_blocking_issue_is_schema_error(self):
        resp = self._valid_response()
        del resp["removeBlockedBy"]["blockingIssue"]
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "missing_blocking_issue" in err
        assert is_schema_error is True

    def test_missing_issue_is_schema_error(self):
        resp = self._valid_response()
        del resp["removeBlockedBy"]["issue"]
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "missing_issue" in err
        assert is_schema_error is True

    def test_issue_id_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["id"] = "WRONG_ISSUE_ID"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_issue_number_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["number"] = 9999
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_blocking_issue_id_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["blockingIssue"]["id"] = "WRONG_BLOCKER_ID"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "blocking_issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_blocking_issue_number_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["blockingIssue"]["number"] = 9999
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "blocking_issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_missing_client_mutation_id_key_is_schema_error(self):
        resp = self._valid_response()
        del resp["removeBlockedBy"]["clientMutationId"]
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "missing_client_mutation_id" in err
        assert is_schema_error is True

    def test_client_mutation_id_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["clientMutationId"] = "wrong-key"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "client_mutation_id_mismatch" in err
        assert is_schema_error is False

    def test_non_string_issue_id_is_schema_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["id"] = 12345
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "issue_id_invalid" in err
        assert is_schema_error is True

    def test_non_int_issue_number_is_schema_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["number"] = "1523"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(resp, **self._KW)
        assert "issue_number_invalid" in err
        assert is_schema_error is True


# =============================================================================
# Issue #1667 review fix_delta P1: pre-mutation tracked-changes precondition
# =============================================================================


class TestIssueDependencyRemovePreMutationTrackedChanges:
    """P1: tracked/staged/untracked changes outside this command's write root
    must be checked BEFORE the remote mutation is attempted, not only after."""

    def test_pre_existing_tracked_changes_block_mutation_before_network_call(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", return_value=(pre_page, "")):
                with patch.object(
                    _exec, "_check_no_tracked_changes", return_value=["M :src/unexpected.ts"]
                ):
                    with patch.object(_exec, "_graphql_call") as mock_gql_after_patch:
                        mock_gql_after_patch.return_value = (pre_page, "")
                        rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        # No removeBlockedBy mutation call was ever made -- the precondition
        # failed before the single mutation attempt.
        for call in mock_gql_after_patch.call_args_list:
            assert "removeBlockedBy" not in call.args[2]


# =============================================================================
# Issue #1667 review fix_delta P1: attempt marker + audit trail on failure
# =============================================================================


class TestIssueDependencyRemoveAttemptMarker:
    """P1: an attempt marker is written BEFORE the remote mutation call, and
    updated on every post-mutation failure path -- an audit trail must exist
    even if the process fails between the mutation and its readback."""

    def test_marker_written_before_mutation_records_mutation_attempted(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project,
            1523,
            ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
            "issue_dependency_remove.marker.json",
        )
        seen_marker_status_before_mutation = {}

        def fake_graphql(gh_bin, env, query, variables):
            if "removeBlockedBy" in query:
                # By this point the attempt marker must already be on disk.
                assert marker_path.exists()
                seen_marker_status_before_mutation["status"] = json.loads(marker_path.read_text())[
                    "status_detail"
                ]
                return None, "gh_api_graphql_errors: simulated_transport_error"
            return pre_page, ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        assert seen_marker_status_before_mutation["status"] == "mutation_attempted"
        # After the failed mutation, the marker is updated to reflect the
        # terminal outcome -- never left stuck at "mutation_attempted".
        final_marker = json.loads(marker_path.read_text())
        assert final_marker["status_detail"] == "transport_or_schema_error"

    def test_marker_records_actor_permission_and_blocker_identity_on_success(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        post_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])
        mutation_response = {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
            }
        }
        responses = iter([pre_page, mutation_response, post_page])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "admin", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc == 0
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project,
            1523,
            ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
            "issue_dependency_remove.marker.json",
        )
        marker = json.loads(marker_path.read_text())
        assert marker["status_detail"] == "removed"
        assert marker["actor_permission"] == "admin"
        assert marker["blocked_issue_id"] == "ISSUE_NODE_BLOCKED"
        assert marker["blocker_node_id"] == "ISSUE_NODE_BLOCKER"


# =============================================================================
# Issue #1667 review fix_delta P1: GH_TOKEN/GITHUB_TOKEN sanitization
# =============================================================================


class TestIssueDependencyRemoveGhEnvSanitization:
    """P1: an ambient GH_TOKEN/GITHUB_TOKEN must never reach the `gh`
    subprocess for issue_dependency.remove -- it would let a trusted-actor
    identity be silently substituted."""

    def test_gh_token_and_github_token_are_stripped(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "ghp_evil_token")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_evil_token_2")
        env = _exec._build_issue_dependency_remove_gh_env()
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env


# =============================================================================
# Issue #1667 review fix_delta P1: closed result-status set (no "failed")
# =============================================================================


class TestIssueDependencyRemoveClosedStatusSetNoFailedValue:
    """P1: issue_dependency.remove's result `status` field must only ever be
    one of {removed, precondition_rejected, transport_or_schema_error,
    postcondition_rejected, already_completed} -- never the undefined
    "failed" value."""

    _CLOSED_STATUS_SET = frozenset(
        {
            "removed",
            "precondition_rejected",
            "transport_or_schema_error",
            "postcondition_rejected",
            "already_completed",
        }
    )

    def test_post_mutation_tracked_changes_status_is_postcondition_rejected(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED",
            1523,
            [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        post_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])
        mutation_response = {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
            }
        }
        responses = iter([pre_page, mutation_response, post_page])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        # First call (pre-mutation precondition check) clean, second call
        # (post-mutation postcondition check) reports an unrelated change.
        tracked_changes_calls = iter([[], ["M :src/unexpected.ts"]])

        def fake_tracked_changes(*args, **kwargs):
            return next(tracked_changes_calls)

        with patch.object(  # noqa: SIM117
            _exec, "_fetch_issue_dependency_remove_actor", return_value=("bot", "write", "")
        ):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(
                    _exec, "_check_no_tracked_changes", side_effect=fake_tracked_changes
                ):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "postcondition_rejected"
        assert out["status"] in self._CLOSED_STATUS_SET
        assert out["status"] != "failed"


# =============================================================================
# Issue #1883: issue_relationship.update (native parent/blockedBy/blocking sync)
# =============================================================================


def _relationship_self_and_parent(self_id, self_number, self_state="OPEN", parent=None):
    return {
        "repository": {
            "issue": {
                "id": self_id,
                "number": self_number,
                "state": self_state,
                "parent": parent,
            }
        }
    }


def _relationship_field_page(self_id, self_number, field, nodes, has_next=False, end_cursor=None):
    return {
        "repository": {
            "issue": {
                "id": self_id,
                "number": self_number,
                field: {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                    "nodes": nodes,
                },
            }
        }
    }


def _rel_node(node_id, number, state="OPEN"):
    return {
        "id": node_id,
        "number": number,
        "state": state,
        "repository": {"nameWithOwner": TRUSTED_REPO},
    }


def _relationship_node_lookup(node_id, number, state="OPEN"):
    return {"repository": {"issue": {"id": node_id, "number": number, "state": state}}}


class _FakeArgs:
    def __init__(self, issue_number=1883, repo=None, dry_run=False):
        self.command_id = "issue_relationship.update"
        self.issue_number = issue_number
        self.repo = repo or TRUSTED_REPO
        self.dry_run = dry_run
        self.output_json = True


def _relationship_ok_calls(reason):  # pragma: no cover - assertion helper
    return reason


def _run_relationship(monkeypatch, input_data, graphql_side_effect, *, permission="admin"):
    results: dict = {}

    def _fail(reason, errors=None, status="error", extra=None):
        results.update(
            {"outcome": "fail", "status": status, "reason": reason, "extra": extra or {}}
        )
        return 1 if status != "error" else 2

    def _ok(extra):
        results.update({"outcome": "ok", **extra})
        return 0

    monkeypatch.setattr(
        _exec, "_fetch_issue_dependency_remove_actor", lambda *_a, **_k: ("bot", permission, "")
    )
    monkeypatch.setattr(_exec, "_check_no_tracked_changes", lambda *_a, **_k: [])
    with patch.object(_exec, "_graphql_call", side_effect=graphql_side_effect):
        _exec._run_issue_relationship_update(
            _FakeArgs(input_data["issue_number"]), input_data, "gh", _fail, _ok
        )
    return results


def _relationship_input(**overrides):
    base = {
        "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
        "issue_number": 1883,
        "repo": TRUSTED_REPO,
        "expected_before": {"parent": None, "blocked_by": [], "blocking": []},
        "parent": {"action": "unchanged", "issue_number": None},
        "add_blocked_by": [],
        "remove_blocked_by": [],
        "add_blocking": [],
        "remove_blocking": [],
        "idempotency_key": "k1",
    }
    base.update(overrides)
    return base


class TestIssueRelationshipUpdateIdempotentNoOp:
    """AC8: current == desired native state produces zero mutation calls."""

    def test_identical_state_produces_no_op_without_mutation_call(self, monkeypatch):
        input_data = _relationship_input()
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),  # self+parent
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),  # blockedBy
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),  # blocking
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "ok"
        assert results["status"] == "no_op"
        assert results["mutation_attempted"] is False


class TestIssueRelationshipUpdateFullPaginationExactSet:
    """AC4/AC5: exhaustive all-page readback drives exact-set comparison for
    blockedBy and blocking, order-independent."""

    def test_add_blocked_by_across_two_pages_matches_desired_exact_set(self, monkeypatch):
        input_data = _relationship_input(
            expected_before={"parent": None, "blocked_by": [10], "blocking": []},
            add_blocked_by=[30],
        )
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),  # pre self+parent
            # pre blockedBy (2 pages)
            (
                _relationship_field_page(
                    "ISELF",
                    1883,
                    "blockedBy",
                    [_rel_node("N10", 10)],
                    has_next=True,
                    end_cursor="C1",
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blockedBy", [], has_next=False), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),  # pre blocking
            (_relationship_node_lookup("N30", 30), ""),  # lookup add target
            (
                {
                    "addBlockedBy": {
                        "issue": {"id": "ISELF", "number": 1883},
                        "blockingIssue": {"id": "N30", "number": 30},
                    }
                },
                "",
            ),  # mutation
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),  # post self+parent
            # post blockedBy (2 pages, unsorted order across pages -- exact-set not list-order)
            (
                _relationship_field_page(
                    "ISELF",
                    1883,
                    "blockedBy",
                    [_rel_node("N30", 30)],
                    has_next=True,
                    end_cursor="C2",
                ),
                "",
            ),
            (
                _relationship_field_page(
                    "ISELF", 1883, "blockedBy", [_rel_node("N10", 10)], has_next=False
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),  # post blocking
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "ok"
        assert results["status"] == "applied"
        assert sorted(results["after"]["blocked_by"]) == [10, 30]

    def test_add_blocking_matches_desired_exact_set(self, monkeypatch):
        input_data = _relationship_input(add_blocking=[9])
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            (_relationship_node_lookup("N9", 9), ""),
            (
                {
                    "addBlockedBy": {
                        "issue": {"id": "N9", "number": 9},
                        "blockingIssue": {"id": "ISELF", "number": 1883},
                    }
                },
                "",
            ),
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", [_rel_node("N9", 9)]), ""),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "ok"
        assert results["status"] == "applied"
        assert results["after"]["blocking"] == [9]


class TestIssueRelationshipUpdateAncestorCycle:
    """AC12: an ancestor cycle (candidate parent's ancestor chain reaches the
    subject issue) is rejected before any parent mutation."""

    def test_ancestor_cycle_rejected_before_mutation(self, monkeypatch):
        input_data = _relationship_input(parent={"action": "set", "issue_number": 200})
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),  # pre self+parent
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            # ancestor walk: 200's parent is 1883 (the subject) -> cycle
            (
                _relationship_self_and_parent(
                    "I200", 200, parent={"id": "ISELF", "number": 1883, "state": "OPEN"}
                ),
                "",
            ),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "fail"
        assert results["status"] == "precondition_rejected"
        assert "ancestor_cycle" in results["reason"]


class TestIssueRelationshipUpdatePartialFailure:
    """AC9: a partial mutation reports before/desired/after and completed/
    pending operations, classified as `partial`."""

    def test_partial_mutation_reports_completed_and_pending(self, monkeypatch):
        input_data = _relationship_input(add_blocked_by=[1, 2])
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            (_relationship_node_lookup("N1", 1), ""),
            (
                {
                    "addBlockedBy": {
                        "issue": {"id": "ISELF", "number": 1883},
                        "blockingIssue": {"id": "N1", "number": 1},
                    }
                },
                "",
            ),
            (None, "gh_api_graphql_failed: transient error"),  # second op's node lookup fails
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),  # post self+parent
            (_relationship_field_page("ISELF", 1883, "blockedBy", [_rel_node("N1", 1)]), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "fail"
        assert results["status"] == "partial"
        assert results["extra"]["completed_operations"] == ["add_blocked_by:1"]
        assert results["extra"]["pending_operations"] == ["add_blocked_by:2"]
        assert results["extra"]["after"]["blocked_by"] == [1]


class TestIssueRelationshipUpdateActorPermission:
    """Actor with insufficient repository permission is rejected before any
    relationship read/mutation is attempted."""

    def test_untrusted_permission_rejected_before_any_graphql_call(self, monkeypatch):
        # Empty side_effect: any _graphql_call invocation would raise
        # StopIteration and fail this test -- proving zero calls happen.
        input_data = _relationship_input(add_blocked_by=[5])
        results = _run_relationship(monkeypatch, input_data, [], permission="read")
        assert results["outcome"] == "fail"
        assert results["status"] == "precondition_rejected"
        assert "credential_actor_not_authorized" in results["reason"]


class TestIssueRelationshipUpdateEffectiveDiffNoOp:
    """PR #1897 P1-2: idempotent no-op is decided by the effective diff
    (current == desired), not by whether the raw add/remove lists are
    empty."""

    def test_redundant_add_remove_produces_zero_graphql_mutations(self, monkeypatch):
        # add_blocked_by=[10] is already present; remove_blocked_by=[99] is
        # not present. Both are redundant -- current == desired -- so this
        # must resolve to no_op with zero GraphQL mutation calls. The
        # side_effect list intentionally only contains the pre-mutation
        # readback calls (self+parent, blockedBy, blocking); any further
        # call would raise StopIteration and fail this test.
        input_data = _relationship_input(
            expected_before={"parent": None, "blocked_by": [10], "blocking": []},
            add_blocked_by=[10],
            remove_blocked_by=[99],
        )
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),
            (_relationship_field_page("ISELF", 1883, "blockedBy", [_rel_node("N10", 10)]), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "ok"
        assert results["status"] == "no_op"
        assert results["mutation_attempted"] is False


class TestIssueRelationshipUpdateParentRebindSingleMutation:
    """PR #1897 P1-3: a parent rebind (old parent -> new parent) must use a
    single addSubIssue(replaceParent: true) mutation, never a
    remove_parent + set_parent pair."""

    def test_parent_rebind_uses_single_add_sub_issue_replace_parent(self, monkeypatch):
        input_data = _relationship_input(
            expected_before={"parent": 1674, "blocked_by": [], "blocking": []},
            parent={"action": "set", "issue_number": 1860},
        )
        calls = [
            (
                _relationship_self_and_parent(
                    "ISELF", 1883, parent={"id": "IOLD", "number": 1674, "state": "OPEN"}
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            # ancestor walk for candidate parent 1860 -- no ancestor cycle.
            (_relationship_self_and_parent("INEW", 1860, parent=None), ""),
            (_relationship_node_lookup("INEW", 1860), ""),  # lookup target for set_parent
            (
                {
                    "addSubIssue": {
                        "issue": {"id": "INEW", "number": 1860},
                        "subIssue": {"id": "ISELF", "number": 1883},
                    }
                },
                "",
            ),
            (
                _relationship_self_and_parent(
                    "ISELF", 1883, parent={"id": "INEW", "number": 1860, "state": "OPEN"}
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "ok"
        assert results["status"] == "applied"
        assert results["completed_operations"] == ["set_parent:1860"]

    def test_parent_rebind_never_calls_remove_sub_issue_first(self, monkeypatch):
        # No removeSubIssue-shaped response is included in this call list --
        # if the implementation issued a remove_parent operation before
        # set_parent, the extra _lookup_relationship_issue_node +
        # _REMOVE_SUB_ISSUE_MUTATION calls would consume the wrong list
        # entries and this test would fail (StopIteration or a shape
        # mismatch), proving only a single addSubIssue mutation is issued.
        input_data = _relationship_input(
            expected_before={"parent": 1674, "blocked_by": [], "blocking": []},
            parent={"action": "set", "issue_number": 1860},
        )
        calls = [
            (
                _relationship_self_and_parent(
                    "ISELF", 1883, parent={"id": "IOLD", "number": 1674, "state": "OPEN"}
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            (_relationship_self_and_parent("INEW", 1860, parent=None), ""),
            (_relationship_node_lookup("INEW", 1860), ""),
            (
                {
                    "addSubIssue": {
                        "issue": {"id": "INEW", "number": 1860},
                        "subIssue": {"id": "ISELF", "number": 1883},
                    }
                },
                "",
            ),
            (
                _relationship_self_and_parent(
                    "ISELF", 1883, parent={"id": "INEW", "number": 1860, "state": "OPEN"}
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "ok"
        assert results["completed_operations"] == ["set_parent:1860"]

    def test_parent_rebind_is_single_replace_parent_mutation(self, monkeypatch):
        """Same scenario as above, verified via the shared _run_relationship
        harness for consistency with the rest of this test module."""
        input_data = _relationship_input(
            expected_before={"parent": 1674, "blocked_by": [], "blocking": []},
            parent={"action": "set", "issue_number": 1860},
        )
        calls = [
            (
                _relationship_self_and_parent(
                    "ISELF", 1883, parent={"id": "IOLD", "number": 1674, "state": "OPEN"}
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            (_relationship_self_and_parent("INEW", 1860, parent=None), ""),
            (_relationship_node_lookup("INEW", 1860), ""),
            (
                {
                    "addSubIssue": {
                        "issue": {"id": "INEW", "number": 1860},
                        "subIssue": {"id": "ISELF", "number": 1883},
                    }
                },
                "",
            ),
            (
                _relationship_self_and_parent(
                    "ISELF", 1883, parent={"id": "INEW", "number": 1860, "state": "OPEN"}
                ),
                "",
            ),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "ok"
        assert len(results["completed_operations"]) == 1
        assert results["completed_operations"][0] == "set_parent:1860"


# =============================================================================
# Issue #2665 AC3: authentication, transport, GraphQL schema, actor
# permission, or postcondition-readback failure in the
# issue_relationship.update route must fail closed -- never report success
# or a relationship-updated outcome. `TestIssueRelationshipUpdateActorPermission`
# (above) already covers the actor-permission branch; the classes below fill
# the remaining fail-closed branches this Issue's AC3 calls out explicitly:
# authenticated-identity fetch failure, pre-mutation readback transport
# failure, post-mutation readback transport failure (attempted-mutation
# state preserved, never silently reclassified), and postcondition final
# state mismatch.
# =============================================================================


class TestIssueRelationshipUpdateAuthenticationFailure:
    """AC3: a failure to resolve the authenticated gh identity (the first
    call the relationship route makes) must reject before any relationship
    read or mutation GraphQL call is attempted."""

    def test_actor_identity_fetch_error_rejected_before_any_graphql_call(self, monkeypatch):
        input_data = _relationship_input(add_blocked_by=[5])
        monkeypatch.setattr(
            _exec,
            "_fetch_issue_dependency_remove_actor",
            lambda *_a, **_k: (None, None, "gh_api_authenticated_user_failed: gh not logged in"),
        )
        monkeypatch.setattr(_exec, "_check_no_tracked_changes", lambda *_a, **_k: [])

        results: dict = {}

        def _fail(reason, errors=None, status="error", extra=None):
            results.update(
                {"outcome": "fail", "status": status, "reason": reason, "extra": extra or {}}
            )
            return 1 if status != "error" else 2

        def _ok(extra):
            results.update({"outcome": "ok", **extra})
            return 0

        # Empty side_effect list: any _graphql_call invocation raises
        # StopIteration and fails this test -- proving zero GraphQL calls
        # happen when the authenticated-identity fetch itself fails.
        with patch.object(_exec, "_graphql_call", side_effect=[]):
            _exec._run_issue_relationship_update(
                _FakeArgs(input_data["issue_number"]), input_data, "gh", _fail, _ok
            )

        assert results["outcome"] == "fail"
        assert results["status"] == "transport_or_schema_error"
        assert results["extra"]["mutation_attempted"] is False
        assert "gh_api_authenticated_user_failed" in results["reason"]

    def test_actor_identity_fetch_error_rejected_before_any_graphql_call_via_real_helper_subprocess_boundary(
        self, monkeypatch
    ):
        """#2682 review P1: the same fail-closed guarantee as above, but
        proven through the REAL `subprocess.run` CLI boundary (only that
        boundary is mocked) -- the exact layer the #2665 credential-context
        bug actually manifested at, not `_graphql_call` itself."""
        captured = _relationship_subprocess_dispatch(monkeypatch, fail_call="auth")
        input_data = _relationship_input(add_blocked_by=[5])

        results = _invoke_relationship_update_capturing_result(input_data)

        assert results["outcome"] == "fail"
        assert results["status"] == "transport_or_schema_error"
        assert results["extra"]["mutation_attempted"] is False
        assert "gh_api_authenticated_user_failed" in results["reason"]
        # Only the identity-fetch `user` call was ever attempted -- no
        # permission call, no GraphQL call: the failure short-circuits
        # before any subsequent subprocess-boundary call.
        assert len(captured) == 1
        assert captured[0][0][4] == "user"


class TestIssueRelationshipUpdatePreReadbackTransportFailure:
    """AC3: a transport/schema failure during the precondition readback
    (before any mutation is attempted) must reject with
    `mutation_attempted: False` -- never treated as a no-op success."""

    def test_pre_self_and_parent_transport_error_rejected_before_mutation(self, monkeypatch):
        input_data = _relationship_input(add_blocked_by=[5])
        calls = [(None, "gh_api_graphql_failed: transient network error")]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "fail"
        assert results["status"] == "transport_or_schema_error"
        assert results["extra"]["mutation_attempted"] is False

    def test_pre_blocked_by_readback_transport_error_rejected_before_mutation(self, monkeypatch):
        input_data = _relationship_input(add_blocked_by=[5])
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),
            (None, "gh_api_graphql_failed: transient network error"),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "fail"
        assert results["status"] == "transport_or_schema_error"
        assert results["extra"]["mutation_attempted"] is False

    def test_pre_self_and_parent_transport_error_rejected_before_mutation_via_real_helper_subprocess_boundary(
        self, monkeypatch
    ):
        """#2682 review P1: real subprocess.run CLI boundary variant of
        `test_pre_self_and_parent_transport_error_rejected_before_mutation`
        above."""
        captured = _relationship_subprocess_dispatch(monkeypatch, fail_call="pre_self_and_parent")
        input_data = _relationship_input(add_blocked_by=[5])

        results = _invoke_relationship_update_capturing_result(input_data)

        assert results["outcome"] == "fail"
        assert results["status"] == "transport_or_schema_error"
        assert results["extra"]["mutation_attempted"] is False
        # actor verification (2 calls) + the failed precondition
        # self_and_parent readback (1) = 3 -- no further readback/mutation
        # subprocess call was attempted once the precondition readback fails.
        assert len(captured) == 3


class TestIssueRelationshipUpdatePostReadbackTransportFailure:
    """AC3: a transport/schema failure during the postcondition readback
    AFTER a mutation was attempted must fail closed while preserving the
    attempted/partial-completion state -- never reclassified as
    `failed_no_mutation` or as an unexecuted transaction."""

    def test_post_self_and_parent_transport_error_after_mutation_attempt_fails_closed(
        self, monkeypatch
    ):
        input_data = _relationship_input(add_blocked_by=[30])
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            (_relationship_node_lookup("N30", 30), ""),
            (
                {
                    "addBlockedBy": {
                        "issue": {"id": "ISELF", "number": 1883},
                        "blockingIssue": {"id": "N30", "number": 30},
                    }
                },
                "",
            ),
            (None, "gh_api_graphql_failed: transient network error"),  # post self+parent fails
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "fail"
        assert results["status"] == "transport_or_schema_error"
        assert results["reason"] == "post_readback_failed_after_mutation_attempt"
        # The mutation WAS attempted (and, per the fixture, completed) --
        # this must survive into the failure result, not be silently
        # dropped or reset to False (AC3: attempted/partial state preserved).
        assert results["extra"]["mutation_attempted"] is True
        assert results["extra"]["completed_operations"] == ["add_blocked_by:30"]

    def test_post_self_and_parent_transport_error_after_mutation_fails_closed_via_real_helper_boundary(
        self, monkeypatch
    ):
        """#2682 review P1: real subprocess.run CLI boundary variant of the
        test above -- proves the mutation actually reached the GraphQL
        `gh api graphql` call and the postcondition readback failure is
        observed at the same subprocess boundary the #2665 bug affected."""
        captured = _relationship_subprocess_dispatch(
            monkeypatch, pre_blocked_by=[10], fail_call="post_self_and_parent"
        )
        input_data = _relationship_input(
            expected_before={"parent": None, "blocked_by": [10], "blocking": []},
            add_blocked_by=[30],
        )

        results = _invoke_relationship_update_capturing_result(input_data)

        assert results["outcome"] == "fail"
        assert results["status"] == "transport_or_schema_error"
        assert results["reason"] == "post_readback_failed_after_mutation_attempt"
        assert results["extra"]["mutation_attempted"] is True
        assert results["extra"]["completed_operations"] == ["add_blocked_by:30"]
        # actor verification (2) + pre readback (3) + node lookup (1) +
        # mutation (1) + failed post self_and_parent readback (1) = 8 -- the
        # post blockedBy/blocking readback calls are never attempted once
        # the post self_and_parent call itself fails.
        assert len(captured) == 8


class TestIssueRelationshipUpdatePostconditionMismatch:
    """AC3: the final-state readback diverging from the desired state after
    a mutation attempt must reject as `postcondition_rejected` -- never
    reported as success -- even though the mutation call itself returned a
    well-formed GraphQL response."""

    def test_after_state_diverges_from_desired_fails_closed_never_success(self, monkeypatch):
        input_data = _relationship_input(add_blocked_by=[30])
        calls = [
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),
            (_relationship_field_page("ISELF", 1883, "blockedBy", []), ""),
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
            (_relationship_node_lookup("N30", 30), ""),
            (
                {
                    "addBlockedBy": {
                        "issue": {"id": "ISELF", "number": 1883},
                        "blockingIssue": {"id": "N30", "number": 30},
                    }
                },
                "",
            ),
            (_relationship_self_and_parent("ISELF", 1883, parent=None), ""),  # post self+parent
            (
                _relationship_field_page("ISELF", 1883, "blockedBy", []),
                "",
            ),  # post blockedBy still empty: mismatch
            (_relationship_field_page("ISELF", 1883, "blocking", []), ""),
        ]
        results = _run_relationship(monkeypatch, input_data, calls)
        assert results["outcome"] == "fail"
        assert results["status"] == "postcondition_rejected"
        assert results["reason"] == "postcondition_final_state_mismatch"
        assert results["extra"]["mutation_attempted"] is True


# =============================================================================
# Issue #2665 AC4: regression coverage that goes through the REAL relationship
# helper (`_run_issue_relationship_update` and its real
# `_fetch_authenticated_login` / `_fetch_issue_dependency_remove_actor` /
# `_graphql_call` / `_fetch_self_and_parent` /
# `_fetch_relationship_field_all_pages` / `_execute_relationship_operation`
# call chain), mocking only the `subprocess.run` CLI boundary -- not
# `_graphql_call` itself (unlike the `_run_relationship` harness used by the
# rest of this file). Every actor-verification, precondition-readback,
# mutation, postcondition-readback, and zero-delta no-op verification call is
# asserted to receive the identical sanitized env and the fixed
# `--hostname github.com` host argv, across config-only / each single token
# carrier / all carriers simultaneously ambient inputs.
# =============================================================================


def _relationship_subprocess_dispatch(
    monkeypatch,
    *,
    login="bot-actor",
    permission="admin",
    self_id="ISELF",
    self_number=1883,
    repo=TRUSTED_REPO,
    pre_blocked_by=(),
    pre_blocking=(),
    add_target_id="N30",
    add_target_number=30,
    post_blocked_by=None,
    post_blocking=None,
    fail_call=None,
):
    """Patch `_exec.subprocess.run` with an argv/payload-keyed dispatcher
    that answers a single add_blocked_by issue_relationship.update flow
    through the REAL `_graphql_call` / actor-fetch code paths. Returns the
    list of captured (argv, kwargs) tuples in call order for assertion.

    `fail_call` (#2682 review P1) optionally injects a single non-zero-rc
    failure at one call site so the fail-closed branches AC3 already covers
    via the `_run_relationship` (mocked-`_graphql_call`) harness can also be
    proven through this REAL subprocess.run CLI boundary -- the exact layer
    the #2665 bug actually manifested at. One of:
    - "auth": the identity-fetch `user` call fails (before any GraphQL call)
    - "pre_self_and_parent": the first (precondition) self+parent readback
      GraphQL call fails (before any mutation is attempted)
    - "post_self_and_parent": the second (postcondition) self+parent
      readback GraphQL call fails (after a mutation was attempted)
    """
    post_blocked_by = (
        list(pre_blocked_by) + [add_target_number] if post_blocked_by is None else post_blocked_by
    )
    post_blocking = list(pre_blocking) if post_blocking is None else post_blocking
    field_call_counts = {"self_and_parent": 0, "blockedBy": 0, "blocking": 0}
    captured: list[tuple[list, dict]] = []

    def _completed(stdout, returncode=0, stderr=""):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )

    def _failed(stderr="gh_api_call_failed: injected transport failure"):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)

    def _dispatch(argv, **kwargs):
        captured.append((list(argv), dict(kwargs)))
        assert argv[0] == "gh"
        assert argv[1:4] == ["api", "--hostname", "github.com"], f"host argv not pinned: {argv!r}"
        if argv[4] == "user":
            if fail_call == "auth":
                return _failed("gh_api_authenticated_user_failed: injected auth failure")
            return _completed(f"{login}\n")
        if argv[4].startswith(f"repos/{repo}/collaborators/") and argv[4].endswith("/permission"):
            return _completed(f"{permission}\n")
        if argv[4] == "graphql":
            payload = json.loads(kwargs["input"])
            query = payload["query"]
            if "parent { id number state }" in query:
                field_call_counts["self_and_parent"] += 1
                occurrence = field_call_counts["self_and_parent"]
                if fail_call == "pre_self_and_parent" and occurrence == 1:
                    return _failed("gh_api_graphql_failed: injected pre-readback transport failure")
                if fail_call == "post_self_and_parent" and occurrence == 2:
                    return _failed(
                        "gh_api_graphql_failed: injected post-readback transport failure"
                    )
                data = _relationship_self_and_parent(self_id, self_number, parent=None)
                return _completed(json.dumps({"data": data}))
            if "blockedBy(first: 50" in query:
                field_call_counts["blockedBy"] += 1
                nums = pre_blocked_by if field_call_counts["blockedBy"] == 1 else post_blocked_by
                nodes = [_rel_node(f"N{n}", n) for n in nums]
                data = _relationship_field_page(self_id, self_number, "blockedBy", nodes)
                return _completed(json.dumps({"data": data}))
            if "blocking(first: 50" in query:
                field_call_counts["blocking"] += 1
                nums = pre_blocking if field_call_counts["blocking"] == 1 else post_blocking
                nodes = [_rel_node(f"N{n}", n) for n in nums]
                data = _relationship_field_page(self_id, self_number, "blocking", nodes)
                return _completed(json.dumps({"data": data}))
            if "addBlockedBy(input:" in query:
                data = {
                    "addBlockedBy": {
                        "issue": {"id": self_id, "number": self_number},
                        "blockingIssue": {"id": add_target_id, "number": add_target_number},
                    }
                }
                return _completed(json.dumps({"data": data}))
            if "issue(number: $number) { id number state }" in query:
                data = _relationship_node_lookup(add_target_id, add_target_number)
                return _completed(json.dumps({"data": data}))
            raise AssertionError(f"unexpected relationship-route graphql query: {query!r}")
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(_exec.subprocess, "run", _dispatch)
    monkeypatch.setattr(_exec, "_check_no_tracked_changes", lambda *_a, **_k: [])
    monkeypatch.setattr(_exec, "_capture_pre_mutation_snapshot", lambda *_a, **_k: (object(), None))
    return captured


def _assert_all_calls_share_sanitized_env_and_fixed_host(captured, expected_env_subset):
    """Every captured gh subprocess call must receive an explicit env=
    kwarg equal to the same sanitized dict (single boundary, no per-call-site
    drift) that preserves exactly the ambient carriers in
    `expected_env_subset` and strips `_METADATA_ENV_NOISE_STRIP_KEYS`. Host
    pinning is asserted separately by `_relationship_subprocess_dispatch`
    (raises on any non `--hostname github.com` argv)."""
    assert captured, "no gh subprocess calls were observed"
    envs = [kwargs.get("env") for _argv, kwargs in captured]
    assert all(env is not None for env in envs), "every gh call must receive an explicit env= kwarg"
    assert all(env == envs[0] for env in envs), (
        "every gh call in the route must share one identical sanitized env"
    )
    for key in _exec._METADATA_ENV_NOISE_STRIP_KEYS:
        assert key not in envs[0]
    for carrier, value in expected_env_subset.items():
        assert envs[0].get(carrier) == value
    for carrier in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"):
        if carrier not in expected_env_subset:
            assert carrier not in envs[0], (
                f"{carrier} must not be synthesized when absent from the ambient env"
            )


def _apply_relationship_ambient_env(monkeypatch, ambient_env):
    """Set exactly `ambient_env` for the three credential-carrier keys.

    #2682 review P1: the carriers NOT present in `ambient_env` must be
    explicitly removed first -- otherwise a "config_only" case only adds
    `GH_CONFIG_DIR` on top of whatever the ambient pytest launcher
    environment already carries (e.g. a real `GH_TOKEN`), and the
    single-carrier claim these parametrized cases assert is never actually
    exercised."""
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in ambient_env.items():
        monkeypatch.setenv(key, value)


def _invoke_relationship_update_capturing_result(input_data):
    """Run `_run_issue_relationship_update` with a `_fail`/`_ok` pair that
    records the single terminal outcome into a plain dict. Shared by the
    real-helper subprocess-boundary tests (success, no-op, and the injected
    fail-closed branches below) so each test body only states its scenario
    and assertions (#2682 review P1)."""
    results: dict = {}

    def _fail(reason, errors=None, status="error", extra=None):
        results.update(
            {"outcome": "fail", "status": status, "reason": reason, "extra": extra or {}}
        )
        return 1 if status != "error" else 2

    def _ok(extra):
        results.update({"outcome": "ok", **extra})
        return 0

    _exec._run_issue_relationship_update(
        _FakeArgs(input_data["issue_number"]), input_data, "gh", _fail, _ok
    )
    return results


@pytest.mark.parametrize(
    "ambient_env,expected_env_subset",
    [
        pytest.param(
            {"GH_CONFIG_DIR": "/fake/native/gh/config"},
            {"GH_CONFIG_DIR": "/fake/native/gh/config"},
            id="config_only",
        ),
        pytest.param(
            {"GH_TOKEN": "ambient-token-1"}, {"GH_TOKEN": "ambient-token-1"}, id="gh_token_only"
        ),
        pytest.param(
            {"GITHUB_TOKEN": "ambient-token-2"},
            {"GITHUB_TOKEN": "ambient-token-2"},
            id="github_token_only",
        ),
        pytest.param(
            {
                "GH_TOKEN": "ambient-token-1",
                "GITHUB_TOKEN": "ambient-token-2",
                "GH_CONFIG_DIR": "/fake/native/gh/config",
            },
            {
                "GH_TOKEN": "ambient-token-1",
                "GITHUB_TOKEN": "ambient-token-2",
                "GH_CONFIG_DIR": "/fake/native/gh/config",
            },
            id="all_carriers_simultaneously",
        ),
    ],
)
class TestIssueRelationshipUpdateRealHelperSubprocessBoundaryEnvParity:
    """AC4: real relationship-helper regression, CLI subprocess boundary
    only mocked, covering config-only / each single token carrier / all
    carriers simultaneously -- for actor verification, precondition
    readback, mutation, and postcondition readback."""

    def test_add_blocked_by_flow_shares_sanitized_env_across_every_call(
        self, monkeypatch, ambient_env, expected_env_subset
    ):
        _apply_relationship_ambient_env(monkeypatch, ambient_env)
        # Noise/redirection overrides that must never reach a `gh`
        # subprocess call, regardless of which credential carriers are set.
        monkeypatch.setenv("GH_HOST", "evil.example.com")
        monkeypatch.setenv("GH_DEBUG", "1")

        captured = _relationship_subprocess_dispatch(monkeypatch, pre_blocked_by=[10])

        input_data = _relationship_input(
            expected_before={"parent": None, "blocked_by": [10], "blocking": []},
            add_blocked_by=[30],
        )
        results = _invoke_relationship_update_capturing_result(input_data)

        assert results["outcome"] == "ok"
        assert results["status"] == "applied"
        assert sorted(results["after"]["blocked_by"]) == [10, 30]
        # actor verification (2 calls) + pre readback (3) + node lookup (1)
        # + mutation (1) + post readback (3) = 10 gh subprocess calls, all
        # sharing the identical sanitized env.
        assert len(captured) == 10
        _assert_all_calls_share_sanitized_env_and_fixed_host(captured, expected_env_subset)

    def test_zero_delta_no_op_flow_shares_sanitized_env_across_every_call(
        self, monkeypatch, ambient_env, expected_env_subset
    ):
        _apply_relationship_ambient_env(monkeypatch, ambient_env)
        monkeypatch.setenv("GH_HOST", "evil.example.com")
        monkeypatch.setenv("GH_DEBUG", "1")

        # add_blocked_by=[10] is already present -- current == desired, so
        # this resolves to a zero-delta no-op with no mutation/post-readback
        # calls (only actor verification + the single precondition readback
        # this route reuses as the no-op comparison).
        captured = _relationship_subprocess_dispatch(monkeypatch, pre_blocked_by=[10])

        input_data = _relationship_input(
            expected_before={"parent": None, "blocked_by": [10], "blocking": []},
            add_blocked_by=[10],
        )
        results = _invoke_relationship_update_capturing_result(input_data)

        assert results["outcome"] == "ok"
        assert results["status"] == "no_op"
        assert results["mutation_attempted"] is False
        # actor verification (2 calls) + precondition readback (3) = 5 gh
        # subprocess calls -- no mutation, no post-readback.
        assert len(captured) == 5
        _assert_all_calls_share_sanitized_env_and_fixed_host(captured, expected_env_subset)


# =============================================================================
# Issue #1116 fix_delta P1-2: the plain `issue_comment.publish` POST path
# (no remote marker found by the pre-mutation precheck) must reconcile a
# lost-response POST via `_readback_by_marker_literal` exactly like the
# PATCH / issue_content.update branches already do, instead of failing
# closed purely because the local process never received the POST's own
# HTTP response.
# =============================================================================


class TestIssueCommentPublishPostFailureReadbackReconciliation:
    def test_post_failure_readback_confirms_reconciled_as_created(self, tmp_project, monkeypatch):
        """A lost POST response is reconciled via readback: the comment was
        actually created remotely, so this must succeed as `created` --
        never a duplicate POST, never a false failure."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        comment_body = "status update <!-- marker-1166 -->"
        marker = "<!-- marker-1166 -->"
        expected_body_sha256 = hashlib.sha256(comment_body.encode()).hexdigest()

        monkeypatch.setattr(_exec, "_find_marker_matches", lambda *a, **k: ([], ""))
        monkeypatch.setattr(
            _exec,
            "_post_gh_comment",
            lambda *a, **k: ("", "", "gh_api_post_comment_response_parse_error: boom"),
        )
        monkeypatch.setattr(
            _exec,
            "_readback_by_marker_literal",
            lambda *a, **k: {
                "comment_id": "1",
                "comment_url": "https://example/comment/1",
                "body_sha256": expected_body_sha256,
            },
        )
        monkeypatch.setattr(_exec, "_check_no_tracked_changes", lambda *a, **k: [])

        args = SimpleNamespace(
            issue_number=1166,
            command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            repo=TRUSTED_REPO,
            dry_run=False,
            output_json=False,
        )
        input_data = {"comment_body": comment_body, "marker": marker}
        _fail, _ok, calls = _capture_fail_ok()

        rc = _exec._run_issue_comment_publish(args, "", input_data, "gh", _fail, _ok)

        assert rc == 0
        assert calls["ok_extra"]["status_detail"] == "created"
        assert calls["ok_extra"]["comment_id"] == "1"
        assert calls["ok_extra"]["body_sha256"] == expected_body_sha256
        assert calls["ok_extra"]["idempotency_marker_written"] is True

    def test_post_failure_readback_marker_not_found_fails_no_mutation(
        self, tmp_project, monkeypatch
    ):
        """A lost POST response reconciled via readback that confirms
        nothing was created remotely (`marker_not_found`) is a clean,
        unambiguous failure -- no `mutation_outcome` extra, so callers know
        no retry-vs-duplicate ambiguity exists."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        comment_body = "status update <!-- marker-1166 -->"
        marker = "<!-- marker-1166 -->"

        monkeypatch.setattr(_exec, "_find_marker_matches", lambda *a, **k: ([], ""))
        monkeypatch.setattr(
            _exec, "_post_gh_comment", lambda *a, **k: ("", "", "gh_api_post_comment_failed_rc_1")
        )
        monkeypatch.setattr(
            _exec, "_readback_by_marker_literal", lambda *a, **k: {"error": "marker_not_found"}
        )

        args = SimpleNamespace(
            issue_number=1166,
            command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            repo=TRUSTED_REPO,
            dry_run=False,
            output_json=False,
        )
        input_data = {"comment_body": comment_body, "marker": marker}
        _fail, _ok, calls = _capture_fail_ok()

        rc = _exec._run_issue_comment_publish(args, "", input_data, "gh", _fail, _ok)

        assert rc == 1
        assert calls["reason"] == "gh_api_post_comment_failed_rc_1"
        assert calls["extra"] is None  # no mutation_outcome extra: confirmed nothing was created

    def test_post_failure_readback_inconclusive_reports_mutation_outcome_unknown(
        self, tmp_project, monkeypatch
    ):
        """A lost POST response whose readback is itself inconclusive
        (transport error, not a confirmed absence) must fail closed while
        reporting `mutation_outcome: unknown` -- POST success/failure
        genuinely cannot be determined either way."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        comment_body = "status update <!-- marker-1166 -->"
        marker = "<!-- marker-1166 -->"

        monkeypatch.setattr(_exec, "_find_marker_matches", lambda *a, **k: ([], ""))
        monkeypatch.setattr(
            _exec,
            "_post_gh_comment",
            lambda *a, **k: ("", "", "gh_api_post_comment_exception: timeout"),
        )
        monkeypatch.setattr(
            _exec, "_readback_by_marker_literal", lambda *a, **k: {"error": "gh_failed_rc_1"}
        )

        args = SimpleNamespace(
            issue_number=1166,
            command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            repo=TRUSTED_REPO,
            dry_run=False,
            output_json=False,
        )
        input_data = {"comment_body": comment_body, "marker": marker}
        _fail, _ok, calls = _capture_fail_ok()

        rc = _exec._run_issue_comment_publish(args, "", input_data, "gh", _fail, _ok)

        assert rc == 1
        assert calls["reason"] == "gh_api_post_comment_exception: timeout"
        assert calls["status"] == "failed"
        assert calls["extra"]["patch_attempted"] is False
        assert calls["extra"]["mutation_outcome"] == "unknown"


# =============================================================================
# Issue #1116 AC8: controlled_skill_mutation_exec.py's own CLI entrypoint must
# be reachable as a genuine OS subprocess, not only via the in-process
# `_exec.main([...])` calls used throughout the rest of this file. This uses
# `--dry-run`, which returns before any `gh` call is made: `_find_gh_bin()`
# in this module pins discovery to a fixed trusted path list that ignores
# PATH by design (Issue #1539 hardening) and is outside this Issue's allowed
# change surface (`_run_issue_comment_publish` + its single-caller helpers
# only). In this environment that fixed path resolves to a real,
# already-authenticated system `gh` -- so no test in this suite may let a
# real subprocess reach an actual `gh` call through it (Issue #1116 AC8
# explicitly forbids ever closing a live Issue). The create/update/no-op/
# reconciliation logic itself remains covered at the function level by the
# rest of this file and by `--dry-run`-independent monkeypatch coverage.
# =============================================================================


class TestControlledExecutorRealSubprocessBoundary:
    def test_dry_run_reachable_as_real_os_subprocess(self):
        script = _REPO_ROOT / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
        issue_dir = _REPO_ROOT / "artifacts" / "1116"
        pre_existing_issue_dir = issue_dir.exists()
        namespace_dir = issue_dir / "issue-metadata" / COMMAND_ID_ISSUE_COMMENT_PUBLISH
        namespace_dir.mkdir(parents=True, exist_ok=True)
        input_file = namespace_dir / "subprocess_boundary_dry_run_input.json"
        input_file.write_text(
            json.dumps(
                {
                    "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
                    "issue_number": 1116,
                    "comment_body": "dry-run subprocess boundary probe <!-- marker-ac8 -->",
                    "marker": "<!-- marker-ac8 -->",
                }
            )
        )
        rel_input = (
            f"artifacts/1116/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/"
            "subprocess_boundary_dry_run_input.json"
        )
        try:
            proc = subprocess.run(  # noqa: PLW1510
                [
                    sys.executable,
                    str(script),
                    "--command-id",
                    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                    "--issue-number",
                    "1116",
                    "--input-file",
                    rel_input,
                    "--repo",
                    TRUSTED_REPO,
                    "--json",
                    "--dry-run",
                ],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            if pre_existing_issue_dir:
                input_file.unlink(missing_ok=True)
            else:
                shutil.rmtree(issue_dir, ignore_errors=True)

        assert proc.returncode == 0, proc.stderr
        parsed = json.loads(proc.stdout)
        assert parsed["status"] == "dry_run_ok"
        assert parsed["command_id"] == COMMAND_ID_ISSUE_COMMENT_PUBLISH
