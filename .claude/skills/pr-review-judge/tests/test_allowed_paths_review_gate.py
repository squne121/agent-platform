#!/usr/bin/env python3
"""Tests for allowed_paths_review_gate.py."""

from pathlib import Path
import subprocess
from unittest.mock import patch
import sys

import pytest

import_path = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(import_path))

from allowed_paths_review_gate import (  # noqa: E402
    AllowedPathsGateEvaluator,
    AllowedPathsMatcher,
    ChangedFileRecord,
    GateStatus,
    SOURCE_GIT_NAME_STATUS_Z,
    SOURCE_PR_FILES_API,
    parse_git_diff_name_status_z,
)


BASE_ARGS = {
    "pr_number": 123,
    "base_ref": "main",
    "base_sha_at_snapshot": "abc123",
    "current_base_sha": "current_base_sha",
    "diff_base_sha": "abc123",
    "head_sha": "def456",
    "reviewed_head_sha": "def456",
    "allowed_paths": ["src/**"],
    "contract_body_sha256": "contract_sha",
    "contract_source_kind": "issue_comment",
    "contract_source_id": "456789",
    "issue_number": 758,
}


def _record(path, status="modified", previous_path=None, source=SOURCE_GIT_NAME_STATUS_Z):
    return ChangedFileRecord(
        path=path,
        status=status,
        previous_path=previous_path,
        source=source,
        provenance_complete=True,
    )


def make_evaluator(**overrides):
    stub_merge_base = overrides.pop("stub_merge_base", True)
    computed_merge_base_sha = overrides.pop("computed_merge_base_sha", None)
    args = dict(BASE_ARGS)
    args.update(overrides)
    if args.get("expected_contract_fingerprint") == "SELF":
        snapshot_args = dict(args)
        snapshot_args.pop("expected_contract_fingerprint", None)
        snapshot_args["expected_contract_fingerprint"] = None
        snapshot = AllowedPathsGateEvaluator(**snapshot_args)
        args["expected_contract_fingerprint"] = snapshot.compute_contract_fingerprint()
    evaluator = AllowedPathsGateEvaluator(**args)
    if stub_merge_base:
        evaluator.compute_current_merge_base_sha = lambda: (
            computed_merge_base_sha or args["diff_base_sha"]
        )
    return evaluator


class TestAllowedPathsMatcher:
    def test_exact_file_match(self):
        assert AllowedPathsMatcher.is_file_allowed("src/main.ts", ["src/main.ts"])

    def test_exact_file_no_partial_match(self):
        assert not AllowedPathsMatcher.is_file_allowed("src/main.tsx", ["src/main.ts"])

    def test_recursive_glob_match(self):
        assert AllowedPathsMatcher.is_file_allowed("src/components/Button.ts", ["src/**"])

    def test_recursive_glob_directory_itself(self):
        assert AllowedPathsMatcher.is_file_allowed("src", ["src/**"])

    def test_single_segment_wildcard(self):
        assert AllowedPathsMatcher.is_file_allowed("docs/README.md", ["docs/*"])
        assert not AllowedPathsMatcher.is_file_allowed("docs/guides/README.md", ["docs/*"])

    def test_parent_directory_traversal_rejected(self):
        assert AllowedPathsMatcher.normalize_path("src/../main.ts") is None

    def test_absolute_path_rejected(self):
        assert AllowedPathsMatcher.normalize_path("/src/main.ts") is None

    def test_backslash_path_is_rejected_fail_closed(self):
        assert AllowedPathsMatcher.normalize_path(r"src\main.ts") is None
        assert not AllowedPathsMatcher.is_file_allowed(r"src\main.ts", ["src/**"])

    def test_literal_regex_metacharacters_do_not_expand(self):
        assert AllowedPathsMatcher.is_file_allowed("docs/v1.0+test/file.md", ["docs/v1.0+test/*"])
        assert AllowedPathsMatcher.is_file_allowed("docs/[draft]/file.md", ["docs/[draft]/*"])
        assert AllowedPathsMatcher.is_file_allowed("packages/foo?bar/file.ts", ["packages/foo?bar/*"])

    def test_invalid_allowed_pattern_is_rejected(self):
        # matcher v2 grammar: mid-path ** is now a valid full segment
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**/nested") == "src/**/nested"
        # partial-segment globs remain invalid (fail-closed)
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**suffix") is None
        assert AllowedPathsMatcher.normalize_allowed_pattern(r"src\**") is None

    # AC1: trailing-slash is normalized to /**
    def test_trailing_slash_normalized_to_double_glob(self):
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui/") == "src/ui/**"

    # AC2: file under trailing-slash directory is allowed
    def test_is_file_allowed_trailing_slash_directory(self):
        assert AllowedPathsMatcher.is_file_allowed("src/ui/HudController.ts", ["src/ui/"])

    # AC3: another trailing-slash directory match
    def test_is_file_allowed_tests_trailing_slash(self):
        assert AllowedPathsMatcher.is_file_allowed("tests/foo.test.ts", ["tests/"])

    # AC4: existing patterns unchanged
    def test_existing_patterns_unchanged(self):
        # exact file
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/main.ts") == "src/main.ts"
        # recursive glob
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui/**") == "src/ui/**"
        # single-level wildcard
        assert AllowedPathsMatcher.normalize_allowed_pattern("docs/*") == "docs/*"
        # matcher v2 grammar: mid-path ** is now a valid full segment
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/**/nested") == "src/**/nested"
        # backslash
        assert AllowedPathsMatcher.normalize_allowed_pattern(r"src\main.ts") is None
        # absolute path
        assert AllowedPathsMatcher.normalize_allowed_pattern("/src/main.ts") is None
        # parent traversal
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/../main.ts") is None

    # AC6: hash equivalence - trailing-slash and /** produce same hash
    def test_compute_allowed_paths_hash_trailing_slash_equals_double_glob(self):
        from allowed_paths_review_gate import AllowedPathsGateEvaluator
        base = {
            "pr_number": 1,
            "base_ref": "main",
            "base_sha_at_snapshot": "abc",
            "current_base_sha": "current_base",
            "diff_base_sha": "abc",
            "head_sha": "def",
            "reviewed_head_sha": "def",
            "contract_body_sha256": "sha",
            "contract_source_kind": "issue_comment",
            "contract_source_id": "123",
            "expected_contract_fingerprint": None,
            "issue_number": 0,
        }
        ev1 = AllowedPathsGateEvaluator(**{**base, "allowed_paths": ["src/ui/"]})
        ev2 = AllowedPathsGateEvaluator(**{**base, "allowed_paths": ["src/ui/**"]})
        assert ev1.compute_allowed_paths_hash() == ev2.compute_allowed_paths_hash()

    # AC7: wildcard + trailing-slash is invalid
    def test_wildcard_trailing_slash_is_invalid(self):
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/*/") is None

    def test_repeated_trailing_slash_is_rejected(self):
        assert AllowedPathsMatcher.normalize_allowed_pattern("src//") is None
        assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui//") is None


class TestContractFingerprintAndExecutionContext:
    def test_fingerprint_contains_contract_info(self):
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        fp = evaluator.compute_contract_fingerprint()
        assert fp["issue_number"] == 758
        assert fp["contract_source_kind"] == "issue_comment"
        assert fp["contract_source_id"] == "456789"
        assert fp["contract_body_sha256"] == "contract_sha"
        assert fp["base_ref"] == "main"
        assert fp["base_sha_at_snapshot"] == "abc123"
        assert "allowed_paths_normalized_sha256" in fp

    def test_fingerprint_changes_on_contract_sha_change(self):
        fp1 = make_evaluator(expected_contract_fingerprint="SELF").compute_contract_fingerprint()
        fp2 = make_evaluator(
            contract_body_sha256="contract_sha_v2",
            expected_contract_fingerprint="SELF").compute_contract_fingerprint(
        )
        assert fp1 != fp2

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_execution_context_does_not_affect_freshness(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert "generated_at" in result.execution_context
        assert "worktree_root" in result.execution_context
        assert "generated_at" not in result.contract_fingerprint
        assert "worktree_root" not in result.contract_fingerprint

    def test_allowed_paths_hash_uses_canonicalized_values(self):
        evaluator1 = make_evaluator(allowed_paths=["./src/**", "src/**"], expected_contract_fingerprint="SELF")
        evaluator2 = make_evaluator(allowed_paths=["src/**"], expected_contract_fingerprint="SELF")
        assert evaluator1.compute_allowed_paths_hash() == evaluator2.compute_allowed_paths_hash()


class TestHeadShaBinding:
    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_head_sha_mismatch_is_indeterminate(self, mock_get_records):
        evaluator = make_evaluator(
            head_sha="current_sha",
            reviewed_head_sha="reviewed_sha",
            expected_contract_fingerprint="SELF"
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        mock_get_records.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_head_sha_match_allows_evaluation(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert len(result.violations) == 0

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_result_has_correct_changed_files_source(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.changed_files_source == SOURCE_GIT_NAME_STATUS_Z
        assert result.diff_base_sha == "abc123"
        assert result.base_sha == "abc123"

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_diff_base_sha_must_equal_current_merge_base(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(
            diff_base_sha="provided_diff_base",
            computed_merge_base_sha="computed_merge_base",
            expected_contract_fingerprint="SELF",
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert result.changed_files_source == "git_diff_unvalidated_diff_base_head"
        assert "provided=provided_diff_base" in result.reason
        assert "computed=computed_merge_base" in result.reason
        mock_get_records.assert_not_called()


class TestAllowedPathsValidation:
    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_changed_file_outside_allowed_paths_is_fail_closed(self, mock_get_records):
        mock_get_records.return_value = [_record("forbidden/bad.ts")]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["src/**", "tests/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert result.violations[0]["file"] == "forbidden/bad.ts"
        assert result.violations[0]["path_role"] == "filename"

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_exact_allowed_file_is_ok(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["src/main.ts"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_recursive_allowed_file_is_ok(self, mock_get_records):
        mock_get_records.return_value = [
            _record("src/components/Button.ts"),
            _record("src/utils/helpers.ts"),
        ]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_contract_snapshot_body_sha_change_is_advisory_stale_not_blocking(self, mock_get_records):
        """Issue #1873 / #1860 Owner Decision: fingerprint drift is advisory-only
        and does not block the gate -- live allowed_paths evaluation is canonical."""
        mock_get_records.return_value = [_record("src/main.ts")]
        expected_fp = make_evaluator(
            contract_body_sha256="old_contract_sha",
            expected_contract_fingerprint="SELF").compute_contract_fingerprint(
        )
        evaluator = make_evaluator(expected_contract_fingerprint=expected_fp)
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert any("stale_snapshot" in w for w in result.warnings)
        mock_get_records.assert_called_once()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_base_sha_at_snapshot_change_is_advisory_stale_not_blocking(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        expected_fp = make_evaluator(
            base_sha_at_snapshot="base_at_snapshot",
            expected_contract_fingerprint="SELF").compute_contract_fingerprint(
        )
        evaluator = make_evaluator(
            base_sha_at_snapshot="base_moved_forward",
            expected_contract_fingerprint=expected_fp,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert any("stale_snapshot" in w for w in result.warnings)

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_diff_base_sha_does_not_affect_freshness(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        expected_fp = make_evaluator(
            base_sha_at_snapshot="base_at_snapshot",
            diff_base_sha="diff_base_at_snapshot",
            expected_contract_fingerprint="SELF",
        ).compute_contract_fingerprint()
        evaluator = make_evaluator(
            base_sha_at_snapshot="base_at_snapshot",
            diff_base_sha="diff_base_after_update_branch",
            expected_contract_fingerprint=expected_fp,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert result.contract_fingerprint["base_sha_at_snapshot"] == "base_at_snapshot"
        assert result.diff_base_sha == "diff_base_after_update_branch"

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_missing_allowed_paths_is_indeterminate(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(allowed_paths=[], expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        mock_get_records.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_missing_expected_contract_fingerprint_is_advisory_not_blocking(self, mock_get_records):
        """Issue #1873 / #1860 Owner Decision: contract snapshot / fingerprint
        are advisory telemetry, not a required blocking input for the Allowed
        Paths gate. Their absence must not indeterminate/block the gate."""
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(expected_contract_fingerprint=None)
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert any("expected_contract_fingerprint_missing" in w for w in result.warnings)
        mock_get_records.assert_called_once()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_missing_contract_source_id_is_advisory_not_blocking(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(contract_source_id="", expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert any("contract_source_kind/source_id_missing" in w for w in result.warnings)
        mock_get_records.assert_called_once()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_invalid_allowed_pattern_is_indeterminate(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        expected_fp = make_evaluator(expected_contract_fingerprint="SELF").compute_contract_fingerprint()
        evaluator = make_evaluator(allowed_paths=["src/**suffix"], expected_contract_fingerprint=expected_fp)
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert "invalid allowed path pattern" in result.reason
        mock_get_records.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_worker_report_is_not_input(self, mock_get_records):
        mock_get_records.return_value = [_record("actual/changed/file.ts")]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed_by_contract/**"])
        result = evaluator.evaluate()
        assert result.changed_files == ["actual/changed/file.ts"]
        assert result.changed_files_source == SOURCE_GIT_NAME_STATUS_Z

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_changed_files_source_does_not_claim_current_merge_base_when_unvalidated(
        self,
        mock_get_records,
    ):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(
            diff_base_sha="provided_diff_base",
            computed_merge_base_sha="computed_merge_base",
            expected_contract_fingerprint="SELF",
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert result.changed_files_source == "git_diff_unvalidated_diff_base_head"
        mock_get_records.assert_not_called()

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_result_schema_includes_diff_base_sha_and_snapshot_fingerprint(self, mock_get_records):
        mock_get_records.return_value = [_record("src/main.ts")]
        evaluator = make_evaluator(
            base_sha_at_snapshot="snapshot_sha",
            diff_base_sha="merge_base_sha",
            expected_contract_fingerprint="SELF",
        )
        result = evaluator.evaluate().to_dict()
        assert result["diff_base_sha"] == "merge_base_sha"
        assert result["base_sha"] == "merge_base_sha"
        assert result["contract_fingerprint"]["base_sha_at_snapshot"] == "snapshot_sha"


class TestChangedFileRecord:
    """AC1: ChangedFileRecord captures filename / status / previous_filename."""

    def test_changed_file_record_fields(self):
        record = ChangedFileRecord(
            path="allowed/new.txt",
            status="renamed",
            previous_path="allowed/old.txt",
            source=SOURCE_GIT_NAME_STATUS_Z,
            provenance_complete=True,
        )
        assert record.path == "allowed/new.txt"
        assert record.status == "renamed"
        assert record.previous_path == "allowed/old.txt"
        assert record.provenance_complete is True

    def test_parse_git_diff_name_status_z_non_rename(self):
        raw = "M\0src/main.ts\0A\0src/new.ts\0"
        records = parse_git_diff_name_status_z(raw, source=SOURCE_GIT_NAME_STATUS_Z)
        assert records[0].path == "src/main.ts"
        assert records[0].status == "modified"
        assert records[0].previous_path is None
        assert records[1].path == "src/new.ts"
        assert records[1].status == "added"

    def test_parse_git_diff_name_status_z_rename(self):
        raw = "R100\0allowed/old.txt\0allowed/new.txt\0"
        records = parse_git_diff_name_status_z(raw, source=SOURCE_GIT_NAME_STATUS_Z)
        assert len(records) == 1
        assert records[0].status == "renamed"
        assert records[0].previous_path == "allowed/old.txt"
        assert records[0].path == "allowed/new.txt"

    def test_parse_git_diff_name_status_z_malformed_rename_raises(self):
        raw = "R100\0allowed/old.txt\0"
        with pytest.raises(ValueError):
            parse_git_diff_name_status_z(raw, source=SOURCE_GIT_NAME_STATUS_Z)

    def test_parse_git_diff_name_status_z_unknown_status_raises(self):
        raw = "Z\0some/path.txt\0"
        with pytest.raises(ValueError):
            parse_git_diff_name_status_z(raw, source=SOURCE_GIT_NAME_STATUS_Z)


class TestRenamePreviousFilenameProvenance:
    """AC2-AC7: rename previous_filename provenance audits both old and new paths."""

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_rename_previous_filename_audits_old_and_new_paths(self, mock_get_records):
        mock_get_records.return_value = [
            _record("allowed/new.txt", status="renamed", previous_path="allowed/old.txt")
        ]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        audited = {(entry["path"], entry["path_role"]) for entry in result.audited_paths}
        assert ("allowed/new.txt", "filename") in audited
        assert ("allowed/old.txt", "previous_filename") in audited
        assert result.changed_file_records[0]["status"] == "renamed"

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_rename_previous_filename_outside_to_inside_fails_closed(self, mock_get_records):
        # Rename destination is inside Allowed Paths but the source is outside —
        # this must NOT be a false green (Issue #1300 core scenario).
        mock_get_records.return_value = [
            _record("allowed/new.txt", status="renamed", previous_path="outside/old.txt")
        ]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert any(
            v["path_role"] == "previous_filename" and v["file"] == "outside/old.txt"
            for v in result.violations
        )

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_rename_previous_filename_inside_to_outside_fails_closed(self, mock_get_records):
        mock_get_records.return_value = [
            _record("outside/new.txt", status="renamed", previous_path="allowed/old.txt")
        ]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert any(
            v["path_role"] == "filename" and v["file"] == "outside/new.txt"
            for v in result.violations
        )

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_rename_previous_filename_inside_to_inside_ok(self, mock_get_records):
        mock_get_records.return_value = [
            _record("allowed/new.txt", status="renamed", previous_path="allowed/old.txt")
        ]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert result.violations == []

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_name_only_source_with_rename_metadata_missing_is_indeterminate(self, mock_get_records):
        # A source that only claims a rename but cannot supply previous_path
        # (e.g. filename-only sources) must never fall back to `ok`.
        mock_get_records.return_value = [
            ChangedFileRecord(
                path="allowed/new.txt",
                status="renamed",
                previous_path=None,
                source="git_diff_current_merge_base_head_name_only",
                provenance_complete=False,
            )
        ]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert "previous_path" in result.reason

    @patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
    def test_previous_filename_invalid_path_is_indeterminate(self, mock_get_records):
        mock_get_records.return_value = [
            _record("allowed/new.txt", status="renamed", previous_path="../escape.txt")
        ]
        evaluator = make_evaluator(expected_contract_fingerprint="SELF", allowed_paths=["allowed/**"])
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value


class TestPrFilesApiSource:
    """AC8: GitHub PR files API pagination / file limit / missing previous_filename."""

    def test_pr_files_pagination_incomplete_is_indeterminate(self):
        evaluator = make_evaluator(
            expected_contract_fingerprint="SELF",
            allowed_paths=["allowed/**"],
            pr_files_data={
                "records": [{"filename": "allowed/new.txt", "status": "modified"}],
                "pagination_complete": False,
                "file_limit_reached": False,
            },
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert "pagination" in result.reason
        assert result.changed_files_source == SOURCE_PR_FILES_API

    def test_pr_files_file_limit_reached_is_indeterminate(self):
        evaluator = make_evaluator(
            expected_contract_fingerprint="SELF",
            allowed_paths=["allowed/**"],
            pr_files_data={
                "records": [{"filename": "allowed/new.txt", "status": "modified"}],
                "pagination_complete": True,
                "file_limit_reached": True,
            },
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value

    def test_pr_files_renamed_record_missing_previous_filename_is_indeterminate(self):
        evaluator = make_evaluator(
            expected_contract_fingerprint="SELF",
            allowed_paths=["allowed/**"],
            pr_files_data={
                "records": [{"filename": "allowed/new.txt", "status": "renamed"}],
                "pagination_complete": True,
                "file_limit_reached": False,
            },
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value

    def test_pr_files_rename_with_previous_filename_audits_both_paths(self):
        evaluator = make_evaluator(
            expected_contract_fingerprint="SELF",
            allowed_paths=["allowed/**"],
            pr_files_data={
                "records": [
                    {
                        "filename": "allowed/new.txt",
                        "status": "renamed",
                        "previous_filename": "allowed/old.txt",
                    }
                ],
                "pagination_complete": True,
                "file_limit_reached": False,
            },
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        audited = {(entry["path"], entry["path_role"]) for entry in result.audited_paths}
        assert ("allowed/new.txt", "filename") in audited
        assert ("allowed/old.txt", "previous_filename") in audited


class TestGitNameStatusZFailClosed:
    """PR #1302 review Blocker 2: `git diff --name-status -M -z` subprocess
    failure / parse errors must be indeterminate, never silently degraded
    into an empty rename map / `modified` status."""

    @patch("allowed_paths_review_gate.subprocess.run")
    def test_git_name_status_z_subprocess_failure_is_indeterminate_not_ok(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=128, cmd=["git", "diff", "--name-status", "-M", "-z"], stderr="fatal: bad revision"
        )
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert result.changed_file_records == []
        assert result.audited_paths == []

    @patch("allowed_paths_review_gate.subprocess.run")
    def test_git_name_status_z_parser_error_is_indeterminate_not_modified(self, mock_run):
        malformed = subprocess.CompletedProcess(
            args=["git", "diff", "--name-status", "-M", "-z"],
            returncode=0,
            stdout="Z\0some/path.txt\0",
            stderr="",
        )
        mock_run.return_value = malformed
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert result.changed_file_records == []
        assert result.audited_paths == []

    @patch("allowed_paths_review_gate.subprocess.run")
    def test_git_name_status_z_unknown_status_in_evaluate_is_indeterminate(self, mock_run):
        unknown_status = subprocess.CompletedProcess(
            args=["git", "diff", "--name-status", "-M", "-z"],
            returncode=0,
            stdout="Q\0some/path.txt\0",
            stderr="",
        )
        mock_run.return_value = unknown_status
        evaluator = make_evaluator(expected_contract_fingerprint="SELF")
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert "unknown git diff --name-status status" in "".join(result.errors)


class TestProvenanceCompletenessAndSourceHardening:
    """PR #1302 review Blocker 3 + High: build_audited_paths() and the PR
    files API adapter must reject provenance_complete=False / insufficient
    or unknown source records as indeterminate, even when the path itself
    would otherwise be inside Allowed Paths (bypass prevention)."""

    def test_provenance_incomplete_record_is_indeterminate_even_if_path_allowed(self):
        records = [
            ChangedFileRecord(
                path="src/main.ts",
                status="modified",
                previous_path=None,
                source=SOURCE_GIT_NAME_STATUS_Z,
                provenance_complete=False,
            )
        ]
        audited, reason = AllowedPathsGateEvaluator.build_audited_paths(records)
        assert audited == []
        assert reason is not None
        assert "provenance incomplete" in reason

    def test_insufficient_source_record_is_indeterminate_even_if_path_allowed(self):
        records = [
            ChangedFileRecord(
                path="src/main.ts",
                status="modified",
                previous_path=None,
                source="git_diff_current_merge_base_head_name_only",
                provenance_complete=True,
            )
        ]
        audited, reason = AllowedPathsGateEvaluator.build_audited_paths(records)
        assert audited == []
        assert reason is not None
        assert "insufficient or unknown changed file source" in reason

    def test_unrecognized_source_record_is_indeterminate(self):
        records = [
            ChangedFileRecord(
                path="src/main.ts",
                status="modified",
                previous_path=None,
                source="some_future_unvetted_source",
                provenance_complete=True,
            )
        ]
        audited, reason = AllowedPathsGateEvaluator.build_audited_paths(records)
        assert audited == []
        assert reason is not None

    def test_pr_files_unknown_status_is_indeterminate(self):
        evaluator = make_evaluator(
            expected_contract_fingerprint="SELF",
            allowed_paths=["allowed/**"],
            pr_files_data={
                "records": [{"filename": "allowed/new.txt", "status": "bogus_status"}],
                "pagination_complete": True,
                "file_limit_reached": False,
            },
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert "unknown pr_files_json status" in "".join(result.errors)

    def test_pr_files_record_with_provenance_incomplete_is_indeterminate(self):
        # Even the preferred oracle must go through the same
        # build_audited_paths() hardening -- a hand-constructed record
        # claiming provenance_complete=False must never be treated as ok.
        records = [
            ChangedFileRecord(
                path="allowed/new.txt",
                status="modified",
                previous_path=None,
                source=SOURCE_PR_FILES_API,
                provenance_complete=False,
            )
        ]
        audited, reason = AllowedPathsGateEvaluator.build_audited_paths(records)
        assert audited == []
        assert reason is not None
        assert "provenance incomplete" in reason



def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(repo: Path, relative_path: str, content: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _create_update_branch_repo(tmp_path: Path) -> dict[str, str | Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")

    _write(repo, "README.md", "base\n")
    base_sha_at_snapshot = _commit(repo, "base")

    _git(repo, "checkout", "-b", "feature")
    _write(repo, "allowed/feature.txt", "feature change\n")
    _commit(repo, "feature change")

    _git(repo, "checkout", "main")
    _write(repo, "outside/unrelated.txt", "main side change\n")
    current_base_tip = _commit(repo, "main unrelated change")

    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-ff", "main", "-m", "update branch")
    head_sha = _git(repo, "rev-parse", "HEAD")

    return {
        "repo": repo,
        "base_sha_at_snapshot": base_sha_at_snapshot,
        "current_base_sha": current_base_tip,
        "diff_base_sha": _git(repo, "merge-base", current_base_tip, head_sha),
        "head_sha": head_sha,
        "reviewed_head_sha": head_sha,
    }


def _create_update_branch_rename_repo(tmp_path: Path) -> dict[str, str | Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")

    _write(repo, "outside/old.txt", "rename target content\n")
    base_sha_at_snapshot = _commit(repo, "base")

    _git(repo, "checkout", "-b", "feature")
    (repo / "allowed").mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "outside/old.txt", "allowed/new.txt")
    _commit(repo, "rename outside to allowed")

    _git(repo, "checkout", "main")
    _write(repo, "main_side/unrelated.txt", "main side change\n")
    current_base_tip = _commit(repo, "main unrelated change")

    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-ff", "main", "-m", "update branch")
    head_sha = _git(repo, "rev-parse", "HEAD")

    return {
        "repo": repo,
        "base_sha_at_snapshot": base_sha_at_snapshot,
        "current_base_sha": current_base_tip,
        "diff_base_sha": _git(repo, "merge-base", current_base_tip, head_sha),
        "head_sha": head_sha,
        "reviewed_head_sha": head_sha,
    }


class TestUpdateBranchGitDag:
    def test_update_branch_no_false_positive(self, tmp_path, monkeypatch):
        fixture = _create_update_branch_repo(tmp_path)
        monkeypatch.chdir(fixture["repo"])
        evaluator = make_evaluator(
            base_sha_at_snapshot=fixture["base_sha_at_snapshot"],
            current_base_sha=fixture["current_base_sha"],
            diff_base_sha=fixture["diff_base_sha"],
            head_sha=fixture["head_sha"],
            reviewed_head_sha=fixture["reviewed_head_sha"],
            allowed_paths=["allowed/**"],
            expected_contract_fingerprint="SELF",
            stub_merge_base=False,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert result.changed_files == ["allowed/feature.txt"]
        assert "outside/unrelated.txt" not in result.changed_files

    def test_update_branch_true_violation_blocks(self, tmp_path, monkeypatch):
        fixture = _create_update_branch_repo(tmp_path)
        repo = fixture["repo"]
        _write(repo, "forbidden/violation.txt", "feature violation\n")
        _commit(repo, "feature violation")
        head_sha = _git(repo, "rev-parse", "HEAD")
        monkeypatch.chdir(repo)
        evaluator = make_evaluator(
            base_sha_at_snapshot=fixture["base_sha_at_snapshot"],
            current_base_sha=fixture["current_base_sha"],
            diff_base_sha=_git(repo, "merge-base", fixture["current_base_sha"], head_sha),
            head_sha=head_sha,
            reviewed_head_sha=head_sha,
            allowed_paths=["allowed/**"],
            expected_contract_fingerprint="SELF",
            stub_merge_base=False,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert any(item["file"] == "forbidden/violation.txt" for item in result.violations)

    def test_stale_snapshot_base_as_diff_base_is_rejected_or_indeterminate(self, tmp_path, monkeypatch):
        fixture = _create_update_branch_repo(tmp_path)
        monkeypatch.chdir(fixture["repo"])
        evaluator = make_evaluator(
            base_sha_at_snapshot=fixture["base_sha_at_snapshot"],
            current_base_sha=fixture["current_base_sha"],
            diff_base_sha=fixture["base_sha_at_snapshot"],
            head_sha=fixture["head_sha"],
            reviewed_head_sha=fixture["reviewed_head_sha"],
            allowed_paths=["allowed/**"],
            expected_contract_fingerprint="SELF",
            stub_merge_base=False,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.INDETERMINATE.value
        assert result.changed_files_source == "git_diff_unvalidated_diff_base_head"
        assert "does not match current merge-base" in result.reason

    def test_git_name_status_z_rename_old_and_new_paths_audited(self, tmp_path, monkeypatch):
        # AC9: real `git diff --name-status -M -z` fixture (deterministic local
        # fallback) — proves the rename is parsed from a genuine git repo, not
        # merely from a hand-constructed ChangedFileRecord.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Test User")
        _git(repo, "config", "user.email", "test@example.com")
        _write(repo, "allowed/old.txt", "identical content\n")
        base_sha = _commit(repo, "base")
        _git(repo, "mv", "allowed/old.txt", "allowed/new.txt")
        head_sha = _commit(repo, "rename")
        monkeypatch.chdir(repo)
        evaluator = make_evaluator(
            base_sha_at_snapshot=base_sha,
            current_base_sha=base_sha,
            diff_base_sha=base_sha,
            head_sha=head_sha,
            reviewed_head_sha=head_sha,
            allowed_paths=["allowed/**"],
            expected_contract_fingerprint="SELF",
            stub_merge_base=False,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.OK.value
        assert result.changed_files_source == SOURCE_GIT_NAME_STATUS_Z
        assert result.changed_file_records[0]["status"] == "renamed"
        audited = {(entry["path"], entry["path_role"]) for entry in result.audited_paths}
        assert ("allowed/new.txt", "filename") in audited
        assert ("allowed/old.txt", "previous_filename") in audited


class TestUpdateBranchRenameDag:
    def test_update_branch_rename_outside_to_inside_no_false_green(self, tmp_path, monkeypatch):
        # AC10: update_branch-style DAG with a rename that crosses the Allowed
        # Paths boundary — base-side unrelated file must not leak into
        # changed_file_records / audited_paths, and the previous_filename
        # violation must fail_closed (not a false green).
        fixture = _create_update_branch_rename_repo(tmp_path)
        monkeypatch.chdir(fixture["repo"])
        evaluator = make_evaluator(
            base_sha_at_snapshot=fixture["base_sha_at_snapshot"],
            current_base_sha=fixture["current_base_sha"],
            diff_base_sha=fixture["diff_base_sha"],
            head_sha=fixture["head_sha"],
            reviewed_head_sha=fixture["reviewed_head_sha"],
            allowed_paths=["allowed/**"],
            expected_contract_fingerprint="SELF",
            stub_merge_base=False,
        )
        result = evaluator.evaluate()
        assert result.status == GateStatus.FAIL_CLOSED.value
        assert any(
            v["path_role"] == "previous_filename" and v["file"] == "outside/old.txt"
            for v in result.violations
        )
        audited_paths = {entry["path"] for entry in result.audited_paths}
        assert "main_side/unrelated.txt" not in audited_paths
        record_paths = {record["path"] for record in result.changed_file_records}
        assert "main_side/unrelated.txt" not in record_paths


class TestSingleSourceOfTruth:
    def test_gate_evaluator_script_exists_in_pr_review_judge(self):
        script_path = Path(__file__).parent.parent / "scripts" / "allowed_paths_review_gate.py"
        assert script_path.exists()

    def test_no_duplicate_gate_evaluator_in_impl_review_loop(self):
        impl_review_loop_path = Path(__file__).parent.parent.parent.parent / "impl-review-loop" / "scripts"
        if impl_review_loop_path.exists():
            for script in impl_review_loop_path.glob("**/*allowed*gate*.py"):
                if script.name != "allowed_paths_review_gate.py":
                    pytest.fail(f"Duplicate gate evaluator found: {script}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
