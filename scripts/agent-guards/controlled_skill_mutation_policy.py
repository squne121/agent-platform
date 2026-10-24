"""
controlled_skill_mutation_policy.py

Shared policy definition for CONTROLLED_SKILL_MUTATION_COMMAND_POLICY.
Consumed by both worktree_scope_guard.py and local_main_branch_guard.py
so the two guards never split-brain on which executor commands are allowed.

This module is SEPARATE from the preflight.run policy (run_refinement_preflight.py).
Command IDs: issue_body.update, issue_comment.publish, contract_snapshot.publish

Issue #1284 extends the same executor lane to issue metadata mutation
(issue_body.update / issue_comment.publish / contract_snapshot.publish) so that
these mutations can run from root/default branch without an issue-specific
worktree. Input file namespace for the new command ids is unified under
``artifacts/{issue_number}/issue-metadata/{command-id}/``.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from repo_identity import load_trusted_repo_slug

# ── Canonical constants ───────────────────────────────────────────────────────

# Single identity source (config/repository_identity.v1.json). An unusable config raises
# RepositoryIdentityError at import, so no registry entry can exist without a trusted slug.
TRUSTED_REPO = load_trusted_repo_slug()
EXECUTOR_SCRIPT = "scripts/agent-guards/controlled_skill_mutation_exec.py"
# Issue #1873: termination_report.publish (COMMAND_ID_PUBLISH) and
# pr_review.publish (COMMAND_ID_PR_REVIEW_PUBLISH) were removed. The final
# summary publish and PR review publish flows are replaced by plain
# `gh issue comment` / `gh pr comment` calls made by the calling orchestrator
# (already allow-listed in .claude/settings.json permissions.allow).
COMMAND_ID_ISSUE_BODY_UPDATE = "issue_body.update"
COMMAND_ID_ISSUE_CONTENT_UPDATE = "issue_content.update"
COMMAND_ID_ISSUE_COMMENT_PUBLISH = "issue_comment.publish"
COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH = "contract_snapshot.publish"
COMMAND_ID_TEST_VERDICT_PUBLISH = "test_verdict.publish"
COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE = "issue_scope_snapshot.materialize"

# Issue #1632: closed-blocker-only GitHub native `blockedBy` relationship
# removal via a fixed GraphQL removeBlockedBy mutation, gated by an
# all-page pre/post readback + node-ID/number/state/set binding.
COMMAND_ID_ISSUE_DEPENDENCY_REMOVE = "issue_dependency.remove"

# Issue #1883: fixed native relationship (parent / blockedBy / blocking) sync
# operation. A single controlled fixed operation; caller never supplies raw
# GraphQL query/REST path/hostname/argv (AC13).
COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE = "issue_relationship.update"

# Issue #1632: hard safety cap on the size of the `blockedBy` set the
# executor will ever readback/compare. Not a page-size knob -- pagination
# itself is fixed at page size 50 (see controlled_skill_mutation_exec.py).
ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS = 500

# Issue #1883: hard safety caps for issue_relationship.update -- bound on any
# single explicit add/remove set size, and on the total node count the
# executor will ever readback/compare for blockedBy/blocking (independent of
# GraphQL page size, which stays fixed at 50; see controlled_skill_mutation_exec.py).
ISSUE_RELATIONSHIP_UPDATE_MAX_SET_SIZE = 200
ISSUE_RELATIONSHIP_UPDATE_MAX_TOTAL_NODES = 500

# Issue #1633: bounded request schema an isolation worktree agent is allowed
# to produce for an Issue comment. The isolation worktree agent never invokes
# `gh` or the controlled executor itself -- it only ever hands back this
# closed-key dict to the canonical main root parent orchestrator, which
# validates it, materializes an ISSUE_COMMENT_PUBLISH_INPUT_V1 file from it,
# and launches controlled_skill_mutation_exec.py --command-id
# issue_comment.publish.
ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA = "ISOLATION_ISSUE_COMMENT_REQUEST_V1"

ISOLATION_ISSUE_COMMENT_REQUEST_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "issue_number",
        "repo",
        "comment_body",
        "marker",
    }
)


def validate_isolation_issue_comment_request(data: object, issue_number: int, repo: str) -> str:
    """Validate a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 dict (Issue #1633).

    Returns "" on success, else a descriptive error string. Enforces a
    closed key set (no keys outside ISOLATION_ISSUE_COMMENT_REQUEST_ALLOWED_KEYS),
    an exact schema match, an issue_number/repo cross-check against the
    caller-declared values, non-empty comment_body/marker strings, and that
    marker is embedded in comment_body. This is the parent-owned bounds check
    on the request an isolation worktree agent is allowed to produce -- it
    runs before any file is written into the issue-scoped input namespace.
    """
    if not isinstance(data, dict):
        return "isolation_issue_comment_request_not_object"

    unknown_keys = set(data.keys()) - ISOLATION_ISSUE_COMMENT_REQUEST_ALLOWED_KEYS
    if unknown_keys:
        return f"isolation_issue_comment_request_unknown_fields: {sorted(unknown_keys)}"

    if data.get("schema") != ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA:
        return f"isolation_issue_comment_request_schema_mismatch: {data.get('schema')!r}"

    req_issue_number = data.get("issue_number")
    if type(req_issue_number) is not int or req_issue_number != issue_number:
        return f"isolation_issue_comment_request_issue_number_mismatch: {req_issue_number!r} != {issue_number!r}"

    req_repo = data.get("repo")
    if req_repo != repo:
        return f"isolation_issue_comment_request_repo_mismatch: {req_repo!r} != {repo!r}"

    comment_body = data.get("comment_body")
    if not isinstance(comment_body, str) or not comment_body:
        return "isolation_issue_comment_request_comment_body_invalid"

    marker = data.get("marker")
    if not isinstance(marker, str) or not marker:
        return "isolation_issue_comment_request_marker_invalid"

    if marker not in comment_body:
        return "isolation_issue_comment_request_marker_not_embedded_in_body"

    return ""


# Issue #1632: ISSUE_DEPENDENCY_REMOVE_INPUT_V1 -- closed-key bounded input
# schema for the issue_dependency.remove command id.
ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA = "ISSUE_DEPENDENCY_REMOVE_INPUT_V1"

ISSUE_DEPENDENCY_REMOVE_INPUT_ALLOWED_KEYS = frozenset(
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


def _is_strict_positive_int(value: object) -> bool:
    """True iff value is a Python int (not bool) and > 0."""
    return type(value) is int and value > 0


def validate_issue_dependency_remove_input(data: object, issue_number: int, repo: str) -> str:
    """Validate a bounded ISSUE_DEPENDENCY_REMOVE_INPUT_V1 dict (Issue #1632).

    Returns "" on success, else a descriptive error string. Enforces a closed
    key set, exact schema match, issue_number/repo cross-check against the
    caller-declared values, a positive target_blocker_number distinct from
    issue_number, non-empty node-id/hash/idempotency-key strings, and a
    sorted, deduplicated, size-capped expected_blocked_by_numbers set that
    contains target_blocker_number. This runs entirely before any GraphQL
    read or mutation -- it is a pure, side-effect-free bounds check on the
    caller-declared expectation, not a readback.
    """
    if not isinstance(data, dict):
        return "issue_dependency_remove_input_not_object"

    unknown_keys = set(data.keys()) - ISSUE_DEPENDENCY_REMOVE_INPUT_ALLOWED_KEYS
    if unknown_keys:
        return f"issue_dependency_remove_input_unknown_fields: {sorted(unknown_keys)}"

    if data.get("schema") != ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA:
        return f"issue_dependency_remove_input_schema_mismatch: {data.get('schema')!r}"

    req_issue_number = data.get("issue_number")
    if not _is_strict_positive_int(req_issue_number) or req_issue_number != issue_number:
        return f"issue_dependency_remove_input_issue_number_mismatch: {req_issue_number!r} != {issue_number!r}"

    req_repo = data.get("repo")
    if req_repo != repo:
        return f"issue_dependency_remove_input_repo_mismatch: {req_repo!r} != {repo!r}"

    target_blocker_number = data.get("target_blocker_number")
    if not _is_strict_positive_int(target_blocker_number):
        return f"issue_dependency_remove_input_target_blocker_number_invalid: {target_blocker_number!r}"
    if target_blocker_number == issue_number:
        return "issue_dependency_remove_input_target_blocker_equals_issue_number"

    for field in ("expected_blocked_issue_node_id", "expected_blocker_node_id"):
        val = data.get(field)
        if not isinstance(val, str) or not val:
            return f"issue_dependency_remove_input_field_invalid: {field!r}"

    expected_numbers = data.get("expected_blocked_by_numbers")
    if not isinstance(expected_numbers, list) or not expected_numbers:
        return "issue_dependency_remove_input_expected_blocked_by_numbers_invalid"
    if len(expected_numbers) > ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS:
        return f"issue_dependency_remove_input_expected_blocked_by_numbers_size_cap_exceeded: {len(expected_numbers)}"
    if not all(_is_strict_positive_int(n) for n in expected_numbers):
        return "issue_dependency_remove_input_expected_blocked_by_numbers_not_all_positive_ints"
    if len(set(expected_numbers)) != len(expected_numbers):
        return "issue_dependency_remove_input_expected_blocked_by_numbers_duplicate"
    if expected_numbers != sorted(expected_numbers):
        return "issue_dependency_remove_input_expected_blocked_by_numbers_not_sorted"
    if target_blocker_number not in expected_numbers:
        return f"issue_dependency_remove_input_target_blocker_not_in_expected_set: {target_blocker_number!r}"

    snapshot_sha256 = data.get("expected_pre_mutation_snapshot_sha256")
    if not isinstance(snapshot_sha256, str) or not snapshot_sha256.startswith("sha256:"):
        return "issue_dependency_remove_input_expected_pre_mutation_snapshot_sha256_invalid"

    idempotency_key = data.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        return "issue_dependency_remove_input_idempotency_key_invalid"

    return ""


# Issue #1883: ISSUE_RELATIONSHIP_UPDATE_INPUT_V1 -- closed-key bounded input
# schema for the issue_relationship.update command id. Covers native `parent`
# (set/remove via addSubIssue(replaceParent:true)/removeSubIssue) and explicit
# `blocked_by`/`blocking` add/remove sets (addBlockedBy/removeBlockedBy, with
# direction swapped for `blocking`). `replace` semantics are intentionally not
# supported -- callers must always express deltas as explicit add_*/remove_*
# sets, and a destructive remove is only ever executed after the executor's
# own live pre-readback confirms `expected_before` (see controlled_skill_mutation_exec.py).
ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA = "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1"

ISSUE_RELATIONSHIP_UPDATE_INPUT_ALLOWED_KEYS = frozenset(
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
ISSUE_RELATIONSHIP_UPDATE_EXPECTED_BEFORE_KEYS = frozenset({"parent", "blocked_by", "blocking"})
ISSUE_RELATIONSHIP_UPDATE_PARENT_KEYS = frozenset({"action", "issue_number"})
ISSUE_RELATIONSHIP_UPDATE_PARENT_ACTIONS = frozenset({"unchanged", "set", "remove"})


def _validate_relationship_number_set(value: object, *, field: str) -> str:
    """Validate a bounded, sorted, deduplicated, strictly-positive-int list.

    Shared shape check for expected_before.blocked_by/blocking and every
    add_*/remove_* field on ISSUE_RELATIONSHIP_UPDATE_INPUT_V1.
    """
    if not isinstance(value, list):
        return f"{field}_not_list"
    if len(value) > ISSUE_RELATIONSHIP_UPDATE_MAX_SET_SIZE:
        return f"{field}_size_cap_exceeded: {len(value)}"
    if not all(_is_strict_positive_int(n) for n in value):
        return f"{field}_not_all_positive_ints"
    if len(set(value)) != len(value):
        return f"{field}_duplicate"
    if value != sorted(value):
        return f"{field}_not_sorted"
    return ""


def validate_issue_relationship_update_input(data: object, issue_number: int, repo: str) -> str:
    """Validate a bounded ISSUE_RELATIONSHIP_UPDATE_INPUT_V1 dict (Issue #1883).

    Returns "" on success, else a descriptive error string. This is a pure,
    side-effect-free static bounds check -- it never performs a GraphQL read
    or mutation. Live readback/drift/ancestor-cycle checks that require
    network access happen in the executor
    (controlled_skill_mutation_exec.py::_run_issue_relationship_update),
    which re-validates via this same function before any mutation attempt
    (defense in depth).

    Enforces the AC12 graph invariants that are decidable statically: closed
    key set, exact schema match, issue_number/repo cross-check, self-parent,
    self-dependency, duplicate/unsorted/bool-as-int/zero/negative entries in
    any add_*/remove_* or expected_before set, add/remove overlap within the
    same relation, and the same target issue number appearing in both a
    blocked_by-related set and a blocking-related set (or as the parent
    target) simultaneously. Ancestor-cycle and cross-repository detection
    (which require live traversal or per-item repo tagging this schema does
    not carry) are out of scope for this pure validator.
    """
    if not isinstance(data, dict):
        return "issue_relationship_update_input_not_object"

    unknown_keys = set(data.keys()) - ISSUE_RELATIONSHIP_UPDATE_INPUT_ALLOWED_KEYS
    if unknown_keys:
        return f"issue_relationship_update_input_unknown_fields: {sorted(unknown_keys)}"

    if data.get("schema") != ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA:
        return f"issue_relationship_update_input_schema_mismatch: {data.get('schema')!r}"

    req_issue_number = data.get("issue_number")
    if not _is_strict_positive_int(req_issue_number) or req_issue_number != issue_number:
        return f"issue_relationship_update_input_issue_number_mismatch: {req_issue_number!r} != {issue_number!r}"

    req_repo = data.get("repo")
    if req_repo != repo:
        return f"issue_relationship_update_input_repo_mismatch: {req_repo!r} != {repo!r}"

    expected_before = data.get("expected_before")
    if not isinstance(expected_before, dict):
        return "issue_relationship_update_input_expected_before_not_object"
    unknown_eb = set(expected_before.keys()) - ISSUE_RELATIONSHIP_UPDATE_EXPECTED_BEFORE_KEYS
    if unknown_eb:
        return (
            f"issue_relationship_update_input_expected_before_unknown_fields: {sorted(unknown_eb)}"
        )
    eb_parent = expected_before.get("parent")
    if eb_parent is not None:
        if not _is_strict_positive_int(eb_parent):
            return "issue_relationship_update_input_expected_before_parent_invalid"
        if eb_parent == issue_number:
            return "issue_relationship_update_input_expected_before_parent_self_reference"
    for eb_field in ("blocked_by", "blocking"):
        eb_err = _validate_relationship_number_set(
            expected_before.get(eb_field),
            field=f"issue_relationship_update_input_expected_before_{eb_field}",
        )
        if eb_err:
            return eb_err
        if issue_number in (expected_before.get(eb_field) or []):
            return f"issue_relationship_update_input_expected_before_{eb_field}_self_reference"

    parent = data.get("parent")
    if not isinstance(parent, dict):
        return "issue_relationship_update_input_parent_not_object"
    unknown_parent = set(parent.keys()) - ISSUE_RELATIONSHIP_UPDATE_PARENT_KEYS
    if unknown_parent:
        return f"issue_relationship_update_input_parent_unknown_fields: {sorted(unknown_parent)}"
    action = parent.get("action")
    if action not in ISSUE_RELATIONSHIP_UPDATE_PARENT_ACTIONS:
        return f"issue_relationship_update_input_parent_action_invalid: {action!r}"
    parent_issue_number = parent.get("issue_number")
    if action == "set":
        if not _is_strict_positive_int(parent_issue_number):
            return "issue_relationship_update_input_parent_set_issue_number_invalid"
        if parent_issue_number == issue_number:
            return "issue_relationship_update_input_parent_set_self_parent"
    elif parent_issue_number is not None:
        return "issue_relationship_update_input_parent_issue_number_must_be_null_unless_set"

    field_values: dict = {}
    for rel_field in ("add_blocked_by", "remove_blocked_by", "add_blocking", "remove_blocking"):
        value = data.get(rel_field)
        rel_err = _validate_relationship_number_set(
            value, field=f"issue_relationship_update_input_{rel_field}"
        )
        if rel_err:
            return rel_err
        if issue_number in value:
            return f"issue_relationship_update_input_{rel_field}_self_reference"
        field_values[rel_field] = set(value)

    if field_values["add_blocked_by"] & field_values["remove_blocked_by"]:
        return "issue_relationship_update_input_add_remove_blocked_by_overlap"
    if field_values["add_blocking"] & field_values["remove_blocking"]:
        return "issue_relationship_update_input_add_remove_blocking_overlap"

    blocked_by_targets = field_values["add_blocked_by"] | field_values["remove_blocked_by"]
    blocking_targets = field_values["add_blocking"] | field_values["remove_blocking"]
    conflict = blocked_by_targets & blocking_targets
    if conflict:
        return f"issue_relationship_update_input_blocked_by_blocking_same_target_conflict: {sorted(conflict)}"

    if action == "set" and parent_issue_number in (blocked_by_targets | blocking_targets):
        return "issue_relationship_update_input_parent_target_conflicts_with_dependency_target"

    idempotency_key = data.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        return "issue_relationship_update_input_idempotency_key_invalid"

    return ""


# Allowed write roots for all commands (relative to project root)
ALLOWED_WRITE_ROOTS = ["artifacts/"]

# Issue #1284: input file namespace for the 3 new command ids is unified under
# artifacts/{issue_number}/issue-metadata/{command-id}/
ISSUE_METADATA_NAMESPACE_SEGMENT = "issue-metadata"

# Per-command-id input schema (AC10). Command id ↔ schema mismatch is denied
# before mutation.
INPUT_SCHEMA_BY_COMMAND: dict = {
    COMMAND_ID_ISSUE_BODY_UPDATE: "ISSUE_BODY_UPDATE_INPUT_V1",
    COMMAND_ID_ISSUE_CONTENT_UPDATE: "ISSUE_CONTENT_UPDATE_INPUT_V1",
    COMMAND_ID_ISSUE_COMMENT_PUBLISH: "ISSUE_COMMENT_PUBLISH_INPUT_V1",
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH: "CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1",
    COMMAND_ID_TEST_VERDICT_PUBLISH: "TEST_VERDICT_PUBLISH_INPUT_V1",
    COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE: "ISSUE_SCOPE_SNAPSHOT_MATERIALIZE_INPUT_V1",
    COMMAND_ID_ISSUE_DEPENDENCY_REMOVE: ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA,
    COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE: ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA,
}

ALL_COMMAND_IDS = frozenset(INPUT_SCHEMA_BY_COMMAND)

# Command ids that keep the legacy (Issue #1166) mandatory LOOP_ISSUE_NUMBER env
# binding. New command ids (Issue #1284 AC15) treat LOOP_ISSUE_NUMBER as
# optional-but-must-match-if-present, since controlled metadata mutation may run
# from root without an issue-specific worktree/session env.
# Issue #1873: termination_report.publish (the only command id that was ever a
# member of this set) was removed, so this set is empty. Kept as a frozenset
# constant (rather than deleted outright) so ENV_BINDING_MANDATORY_COMMAND_IDS
# remains a stable import name for controlled_skill_mutation_exec.py.
ENV_BINDING_MANDATORY_COMMAND_IDS = frozenset()

# Environment variables that the executor sanitizes (removes from child env)
# Issue #1539 fix_delta Blocker 2: GH_HOST / GH_REPO / GH_CONFIG_DIR / GH_DEBUG /
# DEBUG are stripped so an inherited parent-process override cannot redirect
# `gh` subprocess calls to a different host/config/repo or leak debug output.
ENV_SANITIZE_KEYS = [
    "PUBLISH_ARTIFACT_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "GH_EDITOR",
    "EDITOR",
    "VISUAL",
    "BROWSER",
    "GH_HOST",
    "GH_REPO",
    "GH_CONFIG_DIR",
    "GH_DEBUG",
    "DEBUG",
    # Issue #1667 review fix_delta P1: an ambient GH_TOKEN/GITHUB_TOKEN in the
    # parent process environment could let a trusted-actor identity be
    # substituted by whatever credential happens to be set (not necessarily
    # the identity `gh auth status` reports for the stored config). Stripping
    # both env vars forces every controlled mutation subprocess to use the
    # already-authenticated `gh` credential store exclusively.
    "GH_TOKEN",
    "GITHUB_TOKEN",
]

# ── CONTROLLED_SKILL_MUTATION_COMMAND_POLICY ──────────────────────────────────
#
# Canonical registry for controlled remote mutation transactions.
# Each entry is keyed by command_id and specifies:
#   executor_script:      repo-relative path to the single executor
#   allowed_write_roots:  directories (relative) the executor may write to
#   github_mutation:      GitHub mutation parameters
#   postcondition:        what must be true after the executor runs
#   idempotency:          how duplicate runs are detected
#   env_sanitize:         env vars overridden/removed before execution
#
CONTROLLED_SKILL_MUTATION_COMMAND_POLICY: dict = {
    COMMAND_ID_ISSUE_BODY_UPDATE: {
        "command_id": COMMAND_ID_ISSUE_BODY_UPDATE,
        "description": "Update GitHub issue body with stale-write precondition (controlled remote mutation)",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_ISSUE_BODY_UPDATE}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_BODY_UPDATE],
        "github_mutation": {
            "edit_issue_body": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "precondition": {
            "previous_body_sha256_must_match_readback": True,
            "previous_updated_at_must_match_readback": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
            "new_body_sha256_must_match_readback": True,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_ISSUE_BODY_UPDATE}/issue_body_update.marker.json"
            ),
            "marker_field": "new_body_sha256",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_ISSUE_CONTENT_UPDATE: {
        "command_id": COMMAND_ID_ISSUE_CONTENT_UPDATE,
        "description": "Update GitHub issue title and body in one fixed PATCH with readback",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_ISSUE_CONTENT_UPDATE}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_CONTENT_UPDATE],
        "github_mutation": {
            "edit_issue_title_and_body": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
            "fixed_endpoint": "repos/{repo}/issues/{issue_number}",
            "fixed_fields": ["title", "body"],
        },
        "precondition": {
            "previous_title_must_match_readback": True,
            "previous_body_sha256_must_match_readback": True,
            "previous_updated_at_must_match_readback": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
            "new_title_must_match_readback": True,
            "new_body_sha256_must_match_readback": True,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_ISSUE_CONTENT_UPDATE}/issue_content_update.marker.json"
            ),
            "marker_field": "idempotency_key",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_ISSUE_COMMENT_PUBLISH: {
        "command_id": COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        "description": "Publish a GitHub issue comment with marker readback (controlled remote mutation)",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_COMMENT_PUBLISH],
        "github_mutation": {
            "comment_on_issue": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish.marker.json"
            ),
            "marker_field": "comment_id",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH: {
        "command_id": COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
        "description": (
            "Publish Contract Snapshot comment via ensure_contract_snapshot.py "
            "(controlled remote mutation; publisher authority fixed to "
            ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py)"
        ),
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH],
        "publisher_script": (".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"),
        "github_mutation": {
            "comment_on_issue": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH}/contract-snapshot-{{issue_number}}.json"
            ),
            "marker_field": "contract_snapshot_url",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_TEST_VERDICT_PUBLISH: {
        "command_id": COMMAND_ID_TEST_VERDICT_PUBLISH,
        "description": "Publish a receipt-bound TEST_VERDICT comment on a current PR head",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_TEST_VERDICT_PUBLISH}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_TEST_VERDICT_PUBLISH],
        "github_mutation": {
            "comment_on_pull_request": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "precondition": {
            "target_pr_head_sha_must_match_readback": True,
            "linked_issue_body_sha256_must_match_readback": True,
            "producer_receipt_must_bind_target_and_linked_issue": True,
        },
        "postcondition": {
            "target_pr_head_sha_must_match_readback": True,
            "published_comment_author_marker_and_body_must_match": True,
            "no_tracked_source_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_TEST_VERDICT_PUBLISH}/test_verdict_publish.marker.json"
            ),
            "marker_field": "comment_id",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE: {
        "command_id": COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
        "description": "Materialize a live GitHub-bound ISSUE_SCOPE_SNAPSHOT_V1 artifact",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
            f"{COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE],
        "materializer_script": "scripts/agent-guards/materialize_issue_scope_snapshot.py",
        "github_mutation": {"read_issue": True, "requires_repo": TRUSTED_REPO},
        "postcondition": {
            "no_tracked_source_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
            "materializer_provenance_required": True,
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_ISSUE_DEPENDENCY_REMOVE: {
        "command_id": COMMAND_ID_ISSUE_DEPENDENCY_REMOVE,
        "description": (
            "Remove a stale closed-blocker GitHub native blockedBy relationship "
            "via a single fixed GraphQL removeBlockedBy mutation, gated by an "
            "all-page pre/post readback and node-ID/number/state/set binding "
            "(Issue #1632)"
        ),
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_ISSUE_DEPENDENCY_REMOVE}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_DEPENDENCY_REMOVE],
        "github_mutation": {
            "remove_blocked_by": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
            "graphql_only": True,
            "fixed_host": "github.com",
        },
        "precondition": {
            "target_blocker_must_be_closed": True,
            "expected_blocked_by_set_must_match_all_page_readback": True,
            "pre_mutation_snapshot_sha256_must_match_readback": True,
            "credential_actor_must_be_trusted_and_authorized": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
            "target_relationship_removed": True,
            "non_target_relationship_set_unchanged": True,
            "post_snapshot_hash_and_marker_must_match": True,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_ISSUE_DEPENDENCY_REMOVE}/issue_dependency_remove.marker.json"
            ),
            "marker_field": "idempotency_key",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE: {
        "command_id": COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE,
        "description": (
            "Synchronize GitHub native parent / blockedBy / blocking relationships "
            "as an ordered, sequenced, no-automatic-retry saga -- fixed "
            "addSubIssue(replaceParent:true)/removeSubIssue/addBlockedBy/"
            "removeBlockedBy mutations, gated by all-page pre/post readback, "
            "expected_before drift detection, and ancestor-cycle rejection "
            "(Issue #1883)"
        ),
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE],
        "github_mutation": {
            "add_or_remove_sub_issue": True,
            "add_or_remove_blocked_by": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
            "graphql_only": True,
            "fixed_host": "github.com",
        },
        "precondition": {
            "expected_before_must_match_all_page_readback": True,
            "ancestor_cycle_must_be_rejected_before_parent_mutation": True,
            "credential_actor_must_be_trusted_and_authorized": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
            "desired_relationship_set_must_match_all_page_readback_for_applied": True,
        },
        "idempotency": {
            "identical_desired_state_produces_zero_mutation_calls": True,
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
}

# ── Executor argv spec ────────────────────────────────────────────────────────
# Exact argv shape accepted by the executor.
# --flag=value forms are rejected (must use separate tokens).
# Duplicate flags are rejected.
# Unknown flags are rejected.

_EXECUTOR_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "--command-id",
        "--issue-number",
        "--input-file",
        "--repo",
    }
)
_EXECUTOR_BOOL_FLAGS: frozenset[str] = frozenset(
    {
        "--json",
        "--dry-run",
    }
)
# Baseline flags required for every invocation.
_EXECUTOR_REQUIRED_FLAGS: frozenset[str] = frozenset(
    {
        "--command-id",
        "--issue-number",
        "--input-file",
        "--repo",
    }
)

# Shell metacharacters that make a command unparseable / compound
_SHELL_METACHAR_RE = re.compile(r"[;&|<>$`\n\r\0\\(){}*?\[\]!~]")


def _validate_executor_argv(args: list[str]) -> bool:
    """True iff args is an exact, non-redundant argv for the executor.

    Rejects:
    - unknown flags
    - duplicate flags
    - --flag=value forms
    - positional arguments (not preceded by a known flag)
    - value-flags with no following value
    - value-flags whose value looks like another flag

    Does NOT validate semantic constraints (repo slug, file existence, etc.) —
    those are enforced by the executor itself.
    """
    seen: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i]
        # Reject positional arguments (bare tokens not starting with --)
        if not tok.startswith("--"):
            return False
        # Reject --flag=value forms (must use separate tokens)
        if "=" in tok:
            return False
        # Reject duplicates
        if tok in seen:
            return False
        seen.add(tok)

        if tok in _EXECUTOR_BOOL_FLAGS:
            i += 1
            continue
        if tok in _EXECUTOR_VALUE_FLAGS:
            if i + 1 >= len(args):
                return False  # value-flag with no following value
            val = args[i + 1]
            if val.startswith("--"):
                return False  # value looks like another flag
            i += 2
            continue
        # Unknown flag
        return False

    # All baseline required flags must be present
    return _EXECUTOR_REQUIRED_FLAGS.issubset(seen)


def is_controlled_skill_mutation_exec_command(cmd: str, project_root: str) -> bool:
    """Return True iff cmd is an exact controlled_skill_mutation_exec.py invocation.

    Accepted forms:
      uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py [FLAGS]
      python3 scripts/agent-guards/controlled_skill_mutation_exec.py [FLAGS]

    Rejected:
    - shell metacharacters (compound commands, injection)
    - --flag=value forms
    - unknown/duplicate flags
    - missing required flags
    - script path that does not resolve to the canonical executor
    - python -c / /tmp/ scripts
    - bash -c wrappers

    This function is the single shared authority consumed by both
    worktree_scope_guard and local_main_branch_guard (Issue #1166 AC4/AC17).
    """
    if not cmd or not cmd.strip():
        return False

    # Reject any shell metacharacter or separator (prevents injection riding
    # along on a legitimate prefix match)
    if _SHELL_METACHAR_RE.search(cmd):
        return False

    try:
        toks = shlex.split(cmd.strip())
    except ValueError:
        return False

    if not toks:
        return False

    # Determine script token index
    if toks[:3] == ["uv", "run", "python3"]:
        rest = toks[3:]
    elif len(toks) >= 1 and os.path.basename(toks[0]) in ("python3", "python"):
        rest = toks[1:]
    else:
        return False

    if not rest:
        return False
    if rest[0].startswith("-"):
        return False  # reject python -c and bare flags

    script_token = rest[0]
    # Reject /tmp/ scripts and python -c forms
    if script_token.startswith("/tmp/") or script_token == "-c":
        return False
    # Reject inline absolute paths that aren't the executor
    if os.path.isabs(script_token):
        script_real = os.path.realpath(script_token)
    else:
        script_real = os.path.realpath(os.path.join(project_root, script_token))

    # Resolve canonical executor path from project_root
    canonical_executor = os.path.realpath(os.path.join(project_root, EXECUTOR_SCRIPT))
    if script_real != canonical_executor:
        return False

    # Validate argv shape
    return _validate_executor_argv(rest[1:])
