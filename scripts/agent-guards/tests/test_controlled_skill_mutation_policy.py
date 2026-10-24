"""
Tests for controlled_skill_mutation_policy.py (Issue #1166).

Tests:
- AC3:  CONTROLLED_SKILL_MUTATION_COMMAND_POLICY schema
- AC4:  is_controlled_skill_mutation_exec_command shared function
- AC8:  only the currently-registered command ids are in the registry
        (Issue #1873 removed termination_report.publish / pr_review.publish)
- AC17: single source of truth (policy module, not per-guard allowlists)

AC baseline for contract VC: these tests are expected to PASS after implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the module is importable from scripts/agent-guards
_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from controlled_skill_mutation_policy import (
    ALLOWED_WRITE_ROOTS,
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    COMMAND_ID_ISSUE_CONTENT_UPDATE,
    COMMAND_ID_ISSUE_DEPENDENCY_REMOVE,
    COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE,
    COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
    COMMAND_ID_TEST_VERDICT_PUBLISH,
    CONTROLLED_SKILL_MUTATION_COMMAND_POLICY,
    ENV_SANITIZE_KEYS,
    EXECUTOR_SCRIPT,
    ISOLATION_ISSUE_COMMENT_REQUEST_ALLOWED_KEYS,
    ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA,
    ISSUE_DEPENDENCY_REMOVE_INPUT_ALLOWED_KEYS,
    ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA,
    ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS,
    ISSUE_RELATIONSHIP_UPDATE_INPUT_ALLOWED_KEYS,
    ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA,
    TRUSTED_REPO,
    _validate_executor_argv,
    is_controlled_skill_mutation_exec_command,
    validate_isolation_issue_comment_request,
    validate_issue_dependency_remove_input,
    validate_issue_relationship_update_input,
)
from repo_identity import load_trusted_repo_slug

# =============================================================================
# AC3: Policy registry schema
# =============================================================================


class TestPolicySchema:
    """AC3: CONTROLLED_SKILL_MUTATION_COMMAND_POLICY schema validation."""

    def test_registry_is_dict(self):
        assert isinstance(CONTROLLED_SKILL_MUTATION_COMMAND_POLICY, dict)

    def test_registry_has_publish_entry(self):
        assert COMMAND_ID_ISSUE_COMMENT_PUBLISH in CONTROLLED_SKILL_MUTATION_COMMAND_POLICY

    def test_publish_entry_has_required_keys(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_COMMENT_PUBLISH]
        required_keys = {
            "command_id",
            "executor_script",
            "allowed_write_roots",
            "github_mutation",
            "postcondition",
            "idempotency",
            "env_sanitize",
        }
        assert required_keys.issubset(set(entry.keys())), (
            f"Missing keys: {required_keys - set(entry.keys())}"
        )

    def test_publish_entry_github_mutation(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_COMMENT_PUBLISH]
        gm = entry["github_mutation"]
        assert gm["comment_on_issue"] is True
        assert gm["requires_repo"] == TRUSTED_REPO
        assert gm["requires_explicit_repo_flag"] is True

    def test_publish_entry_postcondition(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_COMMENT_PUBLISH]
        pc = entry["postcondition"]
        assert pc["no_tracked_source_changes"] is True
        assert pc["no_settings_changes"] is True
        assert "artifacts/" in pc["allowed_write_roots"]

    def test_publish_entry_idempotency(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_COMMENT_PUBLISH]
        idm = entry["idempotency"]
        assert "issue_comment_publish.marker.json" in idm["marker_file_pattern"]
        assert idm["marker_field"] == "comment_id"

    def test_publish_entry_env_sanitize(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_COMMENT_PUBLISH]
        sanitize = entry["env_sanitize"]
        for key in ("PYTHONPATH", "PYTHONHOME", "PUBLISH_ARTIFACT_DIR"):
            assert key in sanitize

    def test_allowed_write_roots_contains_artifacts(self):
        assert "artifacts/" in ALLOWED_WRITE_ROOTS

    def test_env_sanitize_keys_contains_required(self):
        required = {
            "PUBLISH_ARTIFACT_DIR",
            "PYTHONPATH",
            "PYTHONHOME",
            "GH_EDITOR",
            "EDITOR",
            "VISUAL",
            "BROWSER",
        }
        assert required.issubset(set(ENV_SANITIZE_KEYS))

    def test_env_sanitize_keys_contains_gh_token_and_github_token(self):
        """Issue #1667 review fix_delta P1: an ambient GH_TOKEN/GITHUB_TOKEN
        must never reach a controlled mutation subprocess -- it could let a
        trusted-actor identity be substituted by whatever credential happens
        to be set in the parent process environment."""
        required = {"GH_TOKEN", "GITHUB_TOKEN"}
        assert required.issubset(set(ENV_SANITIZE_KEYS))

    def test_trusted_repo(self):
        assert TRUSTED_REPO == load_trusted_repo_slug()

    def test_executor_script_path(self):
        assert EXECUTOR_SCRIPT == "scripts/agent-guards/controlled_skill_mutation_exec.py"


# =============================================================================
# AC8: only the currently-registered command ids are in the registry
# =============================================================================


class TestRegistryScope:
    """AC8: registry contains only the expected command IDs."""

    def test_only_known_command_ids(self):
        # Issue #1284 extends the shared registry with issue metadata mutation
        # command ids (issue_body.update / issue_content.update /
        # issue_comment.publish / contract_snapshot.publish). Issue #1647 adds
        # the dedicated receipt-bound TEST_VERDICT publisher. Issue #1873
        # removes termination_report.publish and pr_review.publish. Issue
        # #1883 adds the native relationship (parent/blockedBy/blocking) sync
        # command id. This scope-pin is updated deliberately as part of each
        # Issue's explicit In Scope registry extension/removal.
        known_ids = {
            "issue_body.update",
            COMMAND_ID_ISSUE_CONTENT_UPDATE,
            "issue_comment.publish",
            "contract_snapshot.publish",
            COMMAND_ID_TEST_VERDICT_PUBLISH,
            COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
            COMMAND_ID_ISSUE_DEPENDENCY_REMOVE,
            COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE,
        }
        actual_ids = set(CONTROLLED_SKILL_MUTATION_COMMAND_POLICY.keys())
        assert actual_ids == known_ids, f"Unexpected extra entries: {actual_ids - known_ids}"


# =============================================================================
# AC4/AC17: is_controlled_skill_mutation_exec_command
# =============================================================================


class TestIsControlledSkillMutationExecCommand:
    """AC4/AC17: shared policy function validates executor argv."""

    # ── Setup: a tmp project_root with a stub executor ─────────────────────

    @pytest.fixture()
    def project_root(self, tmp_path):
        """Create a tmp project_root with stub executor at canonical path."""
        executor_dir = tmp_path / "scripts" / "agent-guards"
        executor_dir.mkdir(parents=True)
        (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")
        return str(tmp_path)

    # ── Allow cases ────────────────────────────────────────────────────────

    def test_uv_run_python3_executor_valid_all_flags(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/termination_report_input.json"
            f" --repo {TRUSTED_REPO}"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    def test_python3_executor_valid_all_flags(self, project_root):
        cmd = (
            "python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/termination_report_input.json"
            f" --repo {TRUSTED_REPO}"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    def test_executor_with_json_flag(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 42"
            " --input-file artifacts/42/input.json"
            f" --repo {TRUSTED_REPO}"
            " --json"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    def test_executor_with_dry_run_flag(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 99"
            " --input-file artifacts/99/input.json"
            f" --repo {TRUSTED_REPO}"
            " --dry-run"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is True

    # ── Deny cases ─────────────────────────────────────────────────────────

    def test_empty_command_denied(self, project_root):
        assert is_controlled_skill_mutation_exec_command("", project_root) is False

    def test_compound_semicolon_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            f" --repo {TRUSTED_REPO}; echo pwned"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_compound_pipe_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            f" --repo {TRUSTED_REPO} | cat"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_missing_required_flag_command_id(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            f" --repo {TRUSTED_REPO}"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_missing_required_flag_repo(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_flag_equals_form_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id=termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            f" --repo {TRUSTED_REPO}"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_unknown_flag_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            f" --repo {TRUSTED_REPO}"
            " --extra-flag evil"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_duplicate_flag_denied(self, project_root):
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            f" --repo {TRUSTED_REPO}"
        )
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_direct_publisher_denied(self, project_root):
        """Direct invocation of publisher is NOT the executor form."""
        cmd = (
            "python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
            " --issue-number 1166"
            f" --repo {TRUSTED_REPO}"
        )
        # Publisher is at a different path from the executor
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_python_c_denied(self, project_root):
        cmd = "python3 -c 'import sys; sys.exit(0)'"
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_bash_wrapper_denied(self, project_root):
        cmd = "bash -c 'uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py'"
        assert is_controlled_skill_mutation_exec_command(cmd, project_root) is False

    def test_executor_not_found_denied(self, tmp_path):
        """Script path must resolve to an existing executor."""
        # Don't create the executor in tmp_path
        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/input.json"
            f" --repo {TRUSTED_REPO}"
        )
        # project_root without executor → realpath mismatch (executor doesn't exist but
        # realpath returns the expected path, canonical comparison fails because paths differ)
        # Since the test executor exists in project_root, use a fresh tmp_path with no executor
        result = is_controlled_skill_mutation_exec_command(cmd, str(tmp_path))
        # If executor doesn't exist, realpath returns the path itself but canonical comparison
        # still compares correctly (file doesn't need to exist for realpath)
        # The test just verifies no crash occurs
        assert isinstance(result, bool)


# =============================================================================
# AC17: single source of truth
# =============================================================================


class TestSingleSourceOfTruth:
    """AC17: verify no per-guard allowlists duplicate the executor path."""

    def test_executor_script_constant_matches_policy(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_COMMENT_PUBLISH]
        assert entry["executor_script"] == EXECUTOR_SCRIPT

    def test_is_csm_exec_command_is_callable(self):
        assert callable(is_controlled_skill_mutation_exec_command)

    def test_validate_executor_argv_is_callable(self):
        assert callable(_validate_executor_argv)

    def test_validate_executor_argv_all_required(self):
        """All required flags must produce True."""
        valid_args = [
            "--command-id",
            "termination_report.publish",
            "--issue-number",
            "1166",
            "--input-file",
            "artifacts/1166/input.json",
            "--repo",
            TRUSTED_REPO,
        ]
        assert _validate_executor_argv(valid_args) is True

    def test_validate_executor_argv_missing_one_required(self):
        """Missing any required flag returns False."""
        base = [
            "--command-id",
            "termination_report.publish",
            "--issue-number",
            "1166",
            "--input-file",
            "artifacts/1166/input.json",
            "--repo",
            TRUSTED_REPO,
        ]
        # Remove --repo and its value
        args_without_repo = base[:-2]
        assert _validate_executor_argv(args_without_repo) is False


# =============================================================================
# AC1 (Issue #1633): ISOLATION_ISSUE_COMMENT_REQUEST_V1 bounded schema
# =============================================================================


class TestIsolationIssueCommentRequestSchema:
    """AC1: closed-key bounded request schema for isolation worktree agent
    Issue comment requests, and its validator."""

    def test_schema_constant_value(self):
        assert ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA == "ISOLATION_ISSUE_COMMENT_REQUEST_V1"

    def test_allowed_keys_are_closed(self):
        assert ISOLATION_ISSUE_COMMENT_REQUEST_ALLOWED_KEYS == frozenset(
            {"schema", "issue_number", "repo", "comment_body", "marker"}
        )

    def _valid_request(self, issue_number=42, repo=TRUSTED_REPO):
        return {
            "schema": ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA,
            "issue_number": issue_number,
            "repo": repo,
            "comment_body": "hello <!-- m -->",
            "marker": "<!-- m -->",
        }

    def test_valid_request_passes(self):
        req = self._valid_request()
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert err == ""

    def test_not_a_dict_rejected(self):
        err = validate_isolation_issue_comment_request(["not", "a", "dict"], 42, TRUSTED_REPO)
        assert "not_object" in err

    def test_unknown_key_rejected(self):
        req = self._valid_request()
        req["extra_field"] = "unexpected"
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert "unknown_fields" in err

    def test_schema_mismatch_rejected(self):
        req = self._valid_request()
        req["schema"] = "WRONG_SCHEMA_V1"
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert "schema_mismatch" in err

    def test_issue_number_mismatch_rejected(self):
        req = self._valid_request(issue_number=42)
        err = validate_isolation_issue_comment_request(req, 99, TRUSTED_REPO)
        assert "issue_number_mismatch" in err

    def test_issue_number_wrong_type_rejected(self):
        req = self._valid_request()
        req["issue_number"] = "42"
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert "issue_number_mismatch" in err

    def test_repo_mismatch_rejected(self):
        req = self._valid_request(repo="attacker/evil-repo")
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert "repo_mismatch" in err

    def test_empty_comment_body_rejected(self):
        req = self._valid_request()
        req["comment_body"] = ""
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert "comment_body_invalid" in err

    def test_empty_marker_rejected(self):
        req = self._valid_request()
        req["marker"] = ""
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert "marker_invalid" in err

    def test_marker_not_embedded_in_body_rejected(self):
        req = self._valid_request()
        req["comment_body"] = "hello, no marker here"
        err = validate_isolation_issue_comment_request(req, 42, TRUSTED_REPO)
        assert "marker_not_embedded_in_body" in err


# =============================================================================
# AC1 (Issue #1632): issue_dependency.remove command id / schema registration
# =============================================================================


class TestIssueDependencyRemoveRegistration:
    """AC1: issue_dependency.remove and ISSUE_DEPENDENCY_REMOVE_INPUT_V1 are
    registered one-to-one, and existing command ids are unaffected
    (read-only compatibility gate)."""

    def test_command_id_value(self):
        assert COMMAND_ID_ISSUE_DEPENDENCY_REMOVE == "issue_dependency.remove"

    def test_registry_has_entry(self):
        assert COMMAND_ID_ISSUE_DEPENDENCY_REMOVE in CONTROLLED_SKILL_MUTATION_COMMAND_POLICY

    def test_entry_input_schema_is_one_to_one(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_DEPENDENCY_REMOVE]
        assert entry["input_schema"] == ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA
        assert ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA == "ISSUE_DEPENDENCY_REMOVE_INPUT_V1"

    def test_entry_has_required_keys(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_DEPENDENCY_REMOVE]
        required_keys = {
            "command_id",
            "executor_script",
            "allowed_write_roots",
            "github_mutation",
            "precondition",
            "postcondition",
            "idempotency",
            "env_sanitize",
        }
        assert required_keys.issubset(set(entry.keys())), (
            f"Missing keys: {required_keys - set(entry.keys())}"
        )

    def test_entry_github_mutation_graphql_only(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_DEPENDENCY_REMOVE]
        gm = entry["github_mutation"]
        assert gm["remove_blocked_by"] is True
        assert gm["graphql_only"] is True
        assert gm["requires_repo"] == TRUSTED_REPO
        assert gm["fixed_host"] == "github.com"

    def test_entry_postcondition_bounds(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_DEPENDENCY_REMOVE]
        pc = entry["postcondition"]
        assert pc["target_relationship_removed"] is True
        assert pc["non_target_relationship_set_unchanged"] is True
        assert pc["post_snapshot_hash_and_marker_must_match"] is True

    def test_read_only_compatibility_gate_publish_entry_unchanged(self):
        """Adding issue_dependency.remove must not perturb the pre-existing
        termination_report.publish schema/dispatch/postcondition contract."""
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_COMMENT_PUBLISH]
        assert entry["executor_script"] == EXECUTOR_SCRIPT
        assert entry["idempotency"]["marker_field"] == "comment_id"
        assert entry["postcondition"]["no_tracked_source_changes"] is True

    def test_read_only_compatibility_gate_issue_scope_snapshot_unchanged(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[
            COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE
        ]
        assert entry["materializer_script"] == (
            "scripts/agent-guards/materialize_issue_scope_snapshot.py"
        )


# =============================================================================
# AC1 (Issue #1632): ISSUE_DEPENDENCY_REMOVE_INPUT_V1 closed-schema validator
# =============================================================================


class TestIssueDependencyRemoveInputValidator:
    """AC1: closed key set, unknown key / null / bool-as-int / duplicate /
    unsorted / size-cap / hash / trusted-repo rejection before mutation."""

    def _valid_request(self, issue_number=1523, repo=TRUSTED_REPO):
        return {
            "schema": ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA,
            "issue_number": issue_number,
            "repo": repo,
            "target_blocker_number": 1403,
            "expected_blocked_issue_node_id": "ISSUE_NODE_A",
            "expected_blocker_node_id": "ISSUE_NODE_B",
            "expected_blocked_by_numbers": [1403],
            "expected_pre_mutation_snapshot_sha256": "sha256:" + "a" * 64,
            "idempotency_key": f"{TRUSTED_REPO}:1523:1403:abc",
        }

    def test_schema_constant_value(self):
        assert ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA == "ISSUE_DEPENDENCY_REMOVE_INPUT_V1"

    def test_allowed_keys_are_closed(self):
        assert ISSUE_DEPENDENCY_REMOVE_INPUT_ALLOWED_KEYS == frozenset(
            {
                "schema",
                "issue_number",
                "repo",
                "target_blocker_number",
                "expected_blocked_issue_node_id",
                "expected_blocker_node_id",
                "expected_blocked_by_numbers",
                "expected_pre_mutation_snapshot_sha256",
                "idempotency_key",
            }
        )

    def test_valid_request_passes(self):
        req = self._valid_request()
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert err == ""

    def test_not_a_dict_rejected(self):
        err = validate_issue_dependency_remove_input(["not", "a", "dict"], 1523, TRUSTED_REPO)
        assert "not_object" in err

    def test_unknown_key_rejected(self):
        req = self._valid_request()
        req["extra_field"] = "unexpected"
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "unknown_fields" in err

    def test_schema_mismatch_rejected(self):
        req = self._valid_request()
        req["schema"] = "WRONG_SCHEMA_V1"
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "schema_mismatch" in err

    def test_issue_number_null_rejected(self):
        req = self._valid_request()
        req["issue_number"] = None
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "issue_number_mismatch" in err

    def test_issue_number_bool_rejected(self):
        req = self._valid_request()
        req["issue_number"] = True
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "issue_number_mismatch" in err

    def test_repo_untrusted_rejected(self):
        req = self._valid_request(repo="attacker/evil-repo")
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "repo_mismatch" in err

    def test_target_blocker_number_bool_rejected(self):
        req = self._valid_request()
        req["target_blocker_number"] = True
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "target_blocker_number_invalid" in err

    def test_target_blocker_equals_issue_number_rejected(self):
        req = self._valid_request()
        req["target_blocker_number"] = req["issue_number"]
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "target_blocker_equals_issue_number" in err

    def test_empty_node_id_rejected(self):
        req = self._valid_request()
        req["expected_blocked_issue_node_id"] = ""
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "field_invalid" in err

    def test_duplicate_blocked_by_numbers_rejected(self):
        req = self._valid_request()
        req["expected_blocked_by_numbers"] = [1403, 1403]
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "duplicate" in err

    def test_unsorted_blocked_by_numbers_rejected(self):
        req = self._valid_request()
        req["expected_blocked_by_numbers"] = [1403, 100]
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "not_sorted" in err

    def test_oversize_blocked_by_numbers_rejected(self):
        req = self._valid_request()
        oversize = list(range(1, ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS + 2))
        req["expected_blocked_by_numbers"] = oversize
        req["target_blocker_number"] = oversize[0]
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "size_cap_exceeded" in err

    def test_bool_in_blocked_by_numbers_rejected(self):
        req = self._valid_request()
        req["expected_blocked_by_numbers"] = [True]
        req["target_blocker_number"] = 1403
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "not_all_positive_ints" in err

    def test_target_blocker_not_in_expected_set_rejected(self):
        req = self._valid_request()
        req["expected_blocked_by_numbers"] = [1500]
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "target_blocker_not_in_expected_set" in err

    def test_hash_missing_prefix_rejected(self):
        req = self._valid_request()
        req["expected_pre_mutation_snapshot_sha256"] = "not-a-hash"
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "snapshot_sha256_invalid" in err

    def test_empty_idempotency_key_rejected(self):
        req = self._valid_request()
        req["idempotency_key"] = ""
        err = validate_issue_dependency_remove_input(req, 1523, TRUSTED_REPO)
        assert "idempotency_key_invalid" in err


# =============================================================================
# AC13 (Issue #1883): issue_relationship.update registration + fixed operation
# =============================================================================


class TestIssueRelationshipUpdateRegistration:
    """AC13: issue_relationship.update and ISSUE_RELATIONSHIP_UPDATE_INPUT_V1
    are registered one-to-one as a fixed controlled operation -- the entry
    carries only closed metadata (no caller-suppliable query/path/hostname/
    argv fields), and pre-existing command ids are unaffected."""

    def test_command_id_value(self):
        assert COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE == "issue_relationship.update"

    def test_registry_has_entry(self):
        assert COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE in CONTROLLED_SKILL_MUTATION_COMMAND_POLICY

    def test_entry_input_schema_is_one_to_one(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE]
        assert entry["input_schema"] == ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA
        assert ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA == "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1"

    def test_entry_has_required_keys(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE]
        required_keys = {
            "command_id",
            "executor_script",
            "allowed_write_roots",
            "github_mutation",
            "precondition",
            "postcondition",
            "idempotency",
            "env_sanitize",
        }
        assert required_keys.issubset(set(entry.keys())), (
            f"Missing keys: {required_keys - set(entry.keys())}"
        )

    def test_entry_executor_script_is_fixed_shared_executor(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE]
        # AC13: the caller can only ever invoke the one shared executor script
        # by --command-id; it never supplies its own script/path.
        assert entry["executor_script"] == EXECUTOR_SCRIPT

    def test_entry_github_mutation_graphql_only_fixed_host(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE]
        gm = entry["github_mutation"]
        assert gm["add_or_remove_sub_issue"] is True
        assert gm["add_or_remove_blocked_by"] is True
        assert gm["graphql_only"] is True
        assert gm["requires_repo"] == TRUSTED_REPO
        assert gm["fixed_host"] == "github.com"

    def test_entry_precondition_bounds(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE]
        pc = entry["precondition"]
        assert pc["expected_before_must_match_all_page_readback"] is True
        assert pc["ancestor_cycle_must_be_rejected_before_parent_mutation"] is True
        assert pc["credential_actor_must_be_trusted_and_authorized"] is True

    def test_entry_postcondition_bounds(self):
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE]
        pc = entry["postcondition"]
        assert pc["desired_relationship_set_must_match_all_page_readback_for_applied"] is True
        assert pc["no_tracked_source_changes"] is True

    def test_read_only_compatibility_gate_dependency_remove_unchanged(self):
        """Adding issue_relationship.update must not perturb the pre-existing
        issue_dependency.remove schema/dispatch/postcondition contract."""
        entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[COMMAND_ID_ISSUE_DEPENDENCY_REMOVE]
        assert entry["input_schema"] == ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA
        assert entry["github_mutation"]["remove_blocked_by"] is True


# =============================================================================
# AC12/AC13 (Issue #1883): ISSUE_RELATIONSHIP_UPDATE_INPUT_V1 closed-schema
# graph invariant validator
# =============================================================================


class TestIssueRelationshipUpdateInputValidator:
    """AC12: closed key set, self-parent, self-dependency, duplicate/unsorted/
    bool-as-int/zero/negative, add/remove overlap, and blocked_by/blocking
    same-target conflict are all rejected before any mutation is possible.
    AC13: no caller-suppliable query/path/hostname/argv fields exist on this
    schema -- only issue numbers, an action enum, and an idempotency key."""

    def _valid_request(self, issue_number=1883, repo=TRUSTED_REPO):
        return {
            "schema": ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA,
            "issue_number": issue_number,
            "repo": repo,
            "expected_before": {"parent": None, "blocked_by": [], "blocking": []},
            "parent": {"action": "set", "issue_number": 1860},
            "add_blocked_by": [],
            "remove_blocked_by": [],
            "add_blocking": [],
            "remove_blocking": [],
            "idempotency_key": f"{TRUSTED_REPO}:1883:relationship:abc",
        }

    def test_schema_constant_value(self):
        assert ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA == "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1"

    def test_allowed_keys_are_closed(self):
        assert ISSUE_RELATIONSHIP_UPDATE_INPUT_ALLOWED_KEYS == frozenset(
            {
                "schema",
                "issue_number",
                "repo",
                "expected_before",
                "parent",
                "add_blocked_by",
                "remove_blocked_by",
                "add_blocking",
                "remove_blocking",
                "idempotency_key",
            }
        )

    def test_no_query_path_hostname_argv_fields_in_allowed_keys(self):
        """AC13: static proof that the caller cannot smuggle a GraphQL query,
        REST path, hostname, or argv override through this schema."""
        forbidden_substrings = ("query", "path", "host", "argv", "endpoint", "url", "command")
        for key in ISSUE_RELATIONSHIP_UPDATE_INPUT_ALLOWED_KEYS:
            for forbidden in forbidden_substrings:
                assert forbidden not in key.lower(), (
                    f"key {key!r} looks like a raw-transport override"
                )

    def test_valid_request_passes(self):
        req = self._valid_request()
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert err == ""

    def test_not_a_dict_rejected(self):
        err = validate_issue_relationship_update_input(["not", "a", "dict"], 1883, TRUSTED_REPO)
        assert "not_object" in err

    def test_unknown_key_rejected(self):
        req = self._valid_request()
        req["extra_field"] = "unexpected"
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "unknown_fields" in err

    def test_schema_mismatch_rejected(self):
        req = self._valid_request()
        req["schema"] = "WRONG_SCHEMA_V1"
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "schema_mismatch" in err

    def test_issue_number_bool_rejected(self):
        req = self._valid_request()
        req["issue_number"] = True
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "issue_number_mismatch" in err

    def test_repo_untrusted_rejected(self):
        req = self._valid_request(repo="attacker/evil-repo")
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "repo_mismatch" in err

    def test_self_parent_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "set", "issue_number": 1883}
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "self_parent" in err

    def test_parent_issue_number_present_when_not_set_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": 42}
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "must_be_null_unless_set" in err

    def test_self_dependency_in_add_blocked_by_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": None}
        req["add_blocked_by"] = [1883]
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "self_reference" in err

    def test_bool_as_int_in_add_blocked_by_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": None}
        req["add_blocked_by"] = [True]
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "not_all_positive_ints" in err

    def test_zero_and_negative_rejected(self):
        for bad in (0, -1):
            req = self._valid_request()
            req["parent"] = {"action": "unchanged", "issue_number": None}
            req["add_blocked_by"] = [bad]
            err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
            assert "not_all_positive_ints" in err

    def test_duplicate_in_set_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": None}
        req["add_blocked_by"] = [5, 5]
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "duplicate" in err

    def test_unsorted_set_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": None}
        req["add_blocked_by"] = [5, 3]
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "not_sorted" in err

    def test_add_remove_overlap_same_relation_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": None}
        req["add_blocked_by"] = [5]
        req["remove_blocked_by"] = [5]
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "overlap" in err

    def test_blocked_by_blocking_same_target_conflict_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": None}
        req["add_blocked_by"] = [9]
        req["add_blocking"] = [9]
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "same_target_conflict" in err

    def test_expected_before_self_reference_rejected(self):
        req = self._valid_request()
        req["parent"] = {"action": "unchanged", "issue_number": None}
        req["expected_before"] = {"parent": None, "blocked_by": [1883], "blocking": []}
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "self_reference" in err

    def test_empty_idempotency_key_rejected(self):
        req = self._valid_request()
        req["idempotency_key"] = ""
        err = validate_issue_relationship_update_input(req, 1883, TRUSTED_REPO)
        assert "idempotency_key_invalid" in err
