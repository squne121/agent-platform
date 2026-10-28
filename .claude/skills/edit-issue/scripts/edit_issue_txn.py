#!/usr/bin/env python3
"""Deterministic transaction helper for existing-issue body/comment mutation.

Consumes ISSUE_EDIT_TXN_INPUT_V1 and routes mutation through controlled
executor command ids only. Stdout is always a single bounded JSON object.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
CONTROLLED_EXEC = REPO_ROOT / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
GUARD_SCRIPT = SCRIPT_PATH.parent / "guard-issue-body.py"
HYGIENE_SCRIPT = SCRIPT_PATH.parent / "issue_contract_hygiene_autofix.py"
READINESS_SCRIPT = (
    REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
)

# Issue #1883: native relationship (parent / blockedBy / blocking) mutation is
# a fixed controlled-executor operation (issue_relationship.update). The pure,
# side-effect-free graph-invariant validator is imported directly (no gh call,
# no mutation) so this module can reject a malformed desired state before
# ever writing an executor input file. AGENT_GUARDS_IMPORT_ROOT is resolved
# from the real on-disk script location (not the mutable REPO_ROOT module
# attribute tests monkeypatch), since it must always point at the real
# controlled_skill_mutation_policy.py regardless of test fixture overrides.
#
# This import is deliberately deferred (module-level function, not a
# top-level `from ... import ...`) so that title/body-only edit calls that
# never touch native_relationships do not depend on
# scripts/agent-guards/controlled_skill_mutation_policy.py at all. That file
# lives outside the edit-issue skill boundary and is intentionally excluded
# from the fake-git-repo copy used by
# scripts/agent-guards/tests/test_skill_runtime_exec_anchor.py, which only
# swaps in controlled_skill_mutation_exec.py at that boundary. A top-level
# import here raised ModuleNotFoundError for every edit_issue_txn.py
# invocation under that harness, even ones that never use native
# relationships.
AGENT_GUARDS_IMPORT_ROOT = SCRIPT_PATH.parents[4] / "scripts" / "agent-guards"

# Issue #2340 AC1 (P0 credential parity, item 3 in scope): every direct `gh`
# invocation in this module is a pre-read / pre-check that feeds the same
# controlled-executor transaction as the actual mutation
# (`controlled_skill_mutation_exec.py`, invoked below via
# `_invoke_controlled_exec`). That executor's GitHub read/write subprocess
# calls run under `_build_metadata_sanitized_env()`. Before this fix, the
# `gh` calls made directly from this module (`_fetch_issue`,
# `_relationship_capability_preflight`, `_fetch_native_relationships`)
# inherited the fully unsanitized ambient environment, so a pre-read could
# observe a different GitHub identity/host/config than the write it gates.
#
# Issue #2340 fix_delta P0-1 (PR #2357 review, 2026-08-27): this list strips
# execution/log-hygiene NOISE only (`GH_HOST` / `GH_REPO` / `GH_DEBUG` /
# `DEBUG` / editor-browser / `PYTHONPATH`), not the GitHub credential
# carrier. `GH_CONFIG_DIR` / `GH_TOKEN` / `GITHUB_TOKEN` are deliberately
# left intact so this pre-read authenticates under the SAME credential
# context as the Claude-GPT launcher (#2299 / PR #2303) and the paired
# controlled-executor write it gates (see
# `controlled_skill_mutation_exec._METADATA_ENV_NOISE_STRIP_KEYS`).
#
# This list is intentionally duplicated (not imported) from
# `scripts/agent-guards/controlled_skill_mutation_exec._METADATA_ENV_NOISE_STRIP_KEYS`:
# title/body-only edit calls in this module must remain importable without
# the `scripts/agent-guards` module tree present (see the comment on
# `_load_validate_relationship_graph_invariants` below -- that tree is
# excluded from the fake-git-repo harness used by
# `scripts/agent-guards/tests/test_skill_runtime_exec_anchor.py`, which only
# swaps in `controlled_skill_mutation_exec.py` at that boundary). A parity
# test (`scripts/agent-guards/tests/test_controlled_skill_mutation_exec_env_parity.py`)
# asserts both lists stay in sync.
_GH_ENV_SANITIZE_KEYS = (
    "PUBLISH_ARTIFACT_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "GH_EDITOR",
    "EDITOR",
    "VISUAL",
    "BROWSER",
    "GH_HOST",
    "GH_REPO",
    "GH_DEBUG",
    "DEBUG",
)


def _sanitized_gh_env() -> dict[str, str]:
    """Build the sanitized environment used for direct `gh` pre-read/pre-check
    calls in this module (Issue #2340 AC1)."""
    env = os.environ.copy()
    for key in _GH_ENV_SANITIZE_KEYS:
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    env["GH_HOST"] = "github.com"
    return env


def _load_validate_relationship_graph_invariants():
    if str(AGENT_GUARDS_IMPORT_ROOT) not in sys.path:
        sys.path.insert(0, str(AGENT_GUARDS_IMPORT_ROOT))
    from controlled_skill_mutation_policy import (
        validate_issue_relationship_update_input,
    )

    return validate_issue_relationship_update_input

INPUT_SCHEMA = "ISSUE_EDIT_TXN_INPUT_V1"
RESULT_SCHEMA = "ISSUE_EDIT_TXN_RESULT_V1"
READINESS_ALLOWED = {"go", "needs_fix", "human_judgment", "input_or_runtime_error"}
CONTROLLED_EXEC_TIMEOUT_SECONDS = 30
TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "issue_number",
        "repo",
        "new_body_file",
        "readiness_forwarding_payload",
        "comment_mode",
        "expected_previous_body_sha256",
        "expected_previous_updated_at",
        "title_update",
        "native_relationships",
        "rewrite_lane",
        "semantic_rewrite_constraints",
    }
)

# Issue #2316: ISSUE_EDIT_TXN_INPUT_V1 additive extension. rewrite_lane is an
# optional discriminator (omitted == "fail_closed_repair", preserving pre-#2316
# behaviour exactly). semantic_rewrite_constraints is only accepted when
# rewrite_lane == "semantic" (presence-correlation invariant). Its nested
# payload (source_artifact / checked_body_sha256 / findings /
# max_rewrite_attempts / no_progress_route etc.) is a versioned opaque
# envelope produced by join_review_results.py -- edit_issue_txn.py validates
# only the schema_version discriminator and does not deep-validate nested
# fields (responsibility for applying the constraints belongs to
# issue-editor, not this transaction helper).
REWRITE_LANE_VALUES = frozenset({"fail_closed_repair", "semantic"})
DEFAULT_REWRITE_LANE = "fail_closed_repair"
SEMANTIC_REWRITE_CONSTRAINTS_SCHEMA = "SEMANTIC_REWRITE_CONSTRAINTS_V1"

# Issue #1883: ISSUE_EDIT_TXN_INPUT_V1 additive extension. native_relationships
# is optional -- omitting it entirely preserves the pre-#1883 body/comment-only
# transaction behaviour exactly (backward compatible).
NATIVE_RELATIONSHIPS_KEYS = frozenset(
    {"expected_before", "parent", "add_blocked_by", "remove_blocked_by", "add_blocking", "remove_blocking"}
)
NATIVE_EXPECTED_BEFORE_KEYS = frozenset({"parent", "blocked_by", "blocking"})
NATIVE_PARENT_KEYS = frozenset({"action", "issue_number"})
NATIVE_PARENT_ACTIONS = frozenset({"unchanged", "set", "remove"})
DEFAULT_NATIVE_RESULT: dict[str, Any] = {
    "attempted": False,
    "status": "not_run",
    "before": None,
    "desired": None,
    "after": None,
    "completed_operations": [],
    "pending_operations": [],
    "errors": [],
}
READINESS_KEYS = frozenset({"readiness_result"})
READINESS_RESULT_REQUIRED_KEYS = frozenset(
    {
        "status",
        "body_sha256",
        "source_checks",
        "errors",
        "readiness_result_ref",
    }
)
READINESS_RESULT_KEYS = frozenset(
    {
        "status",
        "body_sha256",
        "source_checks",
        "errors",
        "readiness_result_ref",
        "resolution_evidence",
    }
)
TITLE_UPDATE_KEYS = frozenset({"required", "proposed_title", "reason"})
COMMENT_MODE_KEYS = frozenset({"mode", "comment_body_file", "marker"})
MAX_ERROR_ITEMS = 8
MAX_ERROR_MESSAGE = 240
MAX_CHILD_SNIPPET = 160


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded(text: str, limit: int = MAX_ERROR_MESSAGE) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _truncate_errors(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "code": _bounded(str(item.get("code", "error")), 80),
            "message": _bounded(str(item.get("message", ""))),
        }
        for item in errors[:MAX_ERROR_ITEMS]
    ]


def _require_trusted_repo(repo: str) -> None:
    """Fail closed unless `repo` equals the trusted slug from repo_identity.

    Runs before any `gh` call (auth status / issue view included). An unusable
    identity config or module is a rejection, never a skip.
    """
    try:
        # Loaded from the fixed on-disk path on every call (no sys.path / sys.modules lookup), so a
        # shadowing or pre-registered `repo_identity` module can never answer for the trusted slug.
        spec = importlib.util.spec_from_file_location(
            "_edit_issue_txn_repo_identity", AGENT_GUARDS_IMPORT_ROOT / "repo_identity.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError("repo_identity_spec_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        trusted = module.is_trusted_repo(repo)
    except Exception as exc:
        raise ValueError("repo_identity_unavailable") from exc
    if not trusted:
        raise ValueError("repo_untrusted")


def _require_closed_keys(data: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{label}_unknown_keys: {', '.join(unknown)}")


def _validate_input_payload(data: dict[str, Any]) -> None:
    _require_closed_keys(data, TOP_LEVEL_KEYS, "input")
    if data.get("schema") != INPUT_SCHEMA:
        raise ValueError("input_schema_invalid")
    if not isinstance(data.get("issue_number"), int) or isinstance(data["issue_number"], bool) or data["issue_number"] <= 0:
        raise ValueError("issue_number_invalid")
    if not isinstance(data.get("repo"), str) or not data["repo"]:
        raise ValueError("repo_invalid")
    _require_trusted_repo(data["repo"])
    if not isinstance(data.get("new_body_file"), str) or not data["new_body_file"]:
        raise ValueError("new_body_file_invalid")
    if not isinstance(data.get("expected_previous_body_sha256"), str) or not data["expected_previous_body_sha256"]:
        raise ValueError("expected_previous_body_sha256_invalid")
    if not isinstance(data.get("expected_previous_updated_at"), str) or not data["expected_previous_updated_at"]:
        raise ValueError("expected_previous_updated_at_invalid")

    readiness = data.get("readiness_forwarding_payload")
    if not isinstance(readiness, dict):
        raise ValueError("readiness_forwarding_payload_invalid")
    _require_closed_keys(readiness, READINESS_KEYS, "readiness_forwarding_payload")
    readiness_result = readiness.get("readiness_result")
    if not isinstance(readiness_result, dict):
        raise ValueError("readiness_result_invalid")
    _require_closed_keys(readiness_result, READINESS_RESULT_KEYS, "readiness_result")
    missing_readiness_required = sorted(READINESS_RESULT_REQUIRED_KEYS - set(readiness_result))
    if missing_readiness_required:
        raise ValueError(f"readiness_result_missing_required_keys: {', '.join(missing_readiness_required)}")
    if readiness_result.get("status") not in READINESS_ALLOWED:
        raise ValueError("readiness_status_invalid")

    title_update = data.get("title_update")
    if title_update is not None:
        if not isinstance(title_update, dict):
            raise ValueError("title_update_invalid")
        _require_closed_keys(title_update, TITLE_UPDATE_KEYS, "title_update")
        if not isinstance(title_update.get("required"), bool):
            raise ValueError("title_update_required_invalid")
        if title_update["required"]:
            proposed_title = title_update.get("proposed_title")
            reason = title_update.get("reason")
            if (
                not isinstance(proposed_title, str)
                or not proposed_title.strip()
                or any(unicodedata.category(char) == "Cc" for char in proposed_title)
            ):
                raise ValueError("title_update_proposed_title_invalid")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("title_update_reason_invalid")
        elif title_update.get("proposed_title") is not None or title_update.get("reason") is not None:
            raise ValueError("title_update_not_required_fields_must_be_null")

    comment_mode = data.get("comment_mode", {"mode": "skip"})
    if not isinstance(comment_mode, dict):
        raise ValueError("comment_mode_invalid")
    _require_closed_keys(comment_mode, COMMENT_MODE_KEYS, "comment_mode")
    if comment_mode.get("mode", "skip") not in {"skip", "publish"}:
        raise ValueError("comment_mode_mode_invalid")
    if comment_mode.get("mode") == "publish":
        if not isinstance(comment_mode.get("comment_body_file"), str) or not comment_mode["comment_body_file"]:
            raise ValueError("comment_body_file_invalid")
        if not isinstance(comment_mode.get("marker"), str) or not comment_mode["marker"]:
            raise ValueError("comment_marker_invalid")

    # Stage 2-4: native relationship mutation is not an approved agent-platform
    # operation. Any structured input (a noop shape included) is rejected here,
    # before any gh read/auth/mutation. The source handlers below stay present
    # but unreachable from this entrypoint.
    if data.get("native_relationships") is not None:
        raise ValueError("native_relationships_unapproved")

    _validate_rewrite_lane_and_constraints(data)


def _validate_rewrite_lane_and_constraints(data: dict[str, Any]) -> None:
    """Issue #2316: validate the additive rewrite_lane discriminator and its
    presence-correlation invariant with semantic_rewrite_constraints.

    rewrite_lane is optional; omitting it is equivalent to
    "fail_closed_repair" (backward compatible with pre-#2316 callers).
    semantic_rewrite_constraints is only accepted when rewrite_lane ==
    "semantic"; it is rejected outright for any other (including omitted)
    lane. When present, only the schema_version discriminator is validated --
    the nested envelope (source_artifact / checked_body_sha256 / findings /
    max_rewrite_attempts / no_progress_route etc.) is treated as an opaque,
    versioned payload and is not deep-validated here (see Background /
    responsibility-boundary note in the Issue #2316 contract).
    """
    rewrite_lane = data.get("rewrite_lane", DEFAULT_REWRITE_LANE)
    if not isinstance(rewrite_lane, str) or rewrite_lane not in REWRITE_LANE_VALUES:
        raise ValueError("rewrite_lane_invalid")

    # Issue #2316 fix_delta (P1-1): presence and value must be checked
    # separately. data.get(...) alone conflates "key absent" with "key
    # present and explicitly null"; both must be rejected for the
    # semantic lane (absent) and both must be rejected for any other lane
    # (present, whether null or an object).
    constraints_present = "semantic_rewrite_constraints" in data
    constraints = data.get("semantic_rewrite_constraints")
    if rewrite_lane == "semantic":
        if not constraints_present:
            raise ValueError("semantic_rewrite_constraints_required_for_semantic_lane")
        if not isinstance(constraints, dict):
            raise ValueError("semantic_rewrite_constraints_invalid")
        if constraints.get("schema_version") != SEMANTIC_REWRITE_CONSTRAINTS_SCHEMA:
            raise ValueError("semantic_rewrite_constraints_schema_version_invalid")
    else:
        if constraints_present:
            raise ValueError("semantic_rewrite_constraints_forbidden_without_semantic_lane")


def _validate_native_relationships_shape(data: dict[str, Any]) -> None:
    """Structural (schema-shape) validation of the ISSUE_EDIT_TXN_INPUT_V1
    additive native_relationships block. This only checks the closed key set
    and basic types the caller must supply; the full AC12 graph-invariant
    check (self-parent, self-dependency, duplicate/unsorted/bool-as-int,
    conflicting blocked_by/blocking targets) is delegated to
    _validate_relationship_graph_invariants (imported from
    controlled_skill_mutation_policy) once the fully-shaped executor payload
    is assembled in _run_native_relationship_step -- this avoids maintaining
    two independent copies of the same invariant logic.
    """
    _require_closed_keys(data, NATIVE_RELATIONSHIPS_KEYS, "native_relationships")

    expected_before = data.get("expected_before")
    if expected_before is not None:
        if not isinstance(expected_before, dict):
            raise ValueError("native_relationships_expected_before_invalid")
        _require_closed_keys(expected_before, NATIVE_EXPECTED_BEFORE_KEYS, "native_relationships_expected_before")

    parent = data.get("parent", {"action": "unchanged", "issue_number": None})
    if not isinstance(parent, dict):
        raise ValueError("native_relationships_parent_invalid")
    _require_closed_keys(parent, NATIVE_PARENT_KEYS, "native_relationships_parent")
    if parent.get("action", "unchanged") not in NATIVE_PARENT_ACTIONS:
        raise ValueError("native_relationships_parent_action_invalid")

    for field in ("add_blocked_by", "remove_blocked_by", "add_blocking", "remove_blocking"):
        value = data.get(field, [])
        if not isinstance(value, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in value):
            raise ValueError(f"native_relationships_{field}_invalid")


def _relationship_capability_preflight() -> tuple[bool, str]:
    """Read-only environment preflight for native relationship mutation
    (AC10). Distinguishes "cannot even attempt this" (environment blocker --
    binary missing, auth unreachable) from a runtime write-permission
    rejection (403/404), which is classified later by the controlled
    executor's own actor-permission check. Success here is never treated as
    proof of write permission.
    """
    gh = shutil.which("gh")
    if not gh:
        return False, "gh_binary_not_found"
    cp = _run_command([gh, "auth", "status", "--hostname", "github.com"], env=_sanitized_gh_env())
    if cp.returncode != 0:
        return False, "gh_auth_status_unreachable"
    return True, ""


def _fetch_native_relationships(issue_number: int, repo: str) -> tuple[dict[str, Any] | None, str]:
    """Best-effort read-only snapshot of {parent, blocked_by, blocking} used
    by this module to determine expected_before (when the caller omits it)
    and to report a pre-mutation snapshot. Fails closed (returns None) rather
    than silently reporting truncated data if a single page of 100 does not
    exhaust blockedBy/blocking -- the authoritative full-pagination readback
    happens inside the controlled executor (issue_relationship.update).
    """
    gh = shutil.which("gh") or "gh"
    owner, _, name = repo.partition("/")
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){"
        "parent{number} "
        "blockedBy(first:100){pageInfo{hasNextPage} totalCount nodes{number}} "
        "blocking(first:100){pageInfo{hasNextPage} totalCount nodes{number}}"
        "}}}"
    )
    cp = _run_command(
        [
            gh,
            "api",
            "graphql",
            "-f",
            "query=\n" + query,
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={issue_number}",
        ],
        env=_sanitized_gh_env(),
    )
    if cp.returncode != 0:
        return None, _bounded(cp.stderr.strip() or cp.stdout.strip())
    try:
        payload = json.loads(cp.stdout)
        issue = payload["data"]["repository"]["issue"]
        parent = issue.get("parent")
        parent_number = parent.get("number") if isinstance(parent, dict) else None
        blocked_by_payload = issue.get("blockedBy") or {}
        blocking_payload = issue.get("blocking") or {}
        if (blocked_by_payload.get("pageInfo") or {}).get("hasNextPage"):
            return None, "blocked_by_pagination_incomplete_for_readonly_snapshot"
        if (blocking_payload.get("pageInfo") or {}).get("hasNextPage"):
            return None, "blocking_pagination_incomplete_for_readonly_snapshot"
        blocked_by = sorted(n["number"] for n in blocked_by_payload.get("nodes", []))
        blocking = sorted(n["number"] for n in blocking_payload.get("nodes", []))
        if blocked_by_payload.get("totalCount") not in (None, len(blocked_by)):
            return None, "blocked_by_total_count_mismatch"
        if blocking_payload.get("totalCount") not in (None, len(blocking)):
            return None, "blocking_total_count_mismatch"
        return {"parent": parent_number, "blocked_by": blocked_by, "blocking": blocking}, ""
    except (KeyError, TypeError, json.JSONDecodeError):
        return None, "gh_graphql_non_json_or_missing_fields"


def _compute_desired_relationship_snapshot(executor_payload: dict[str, Any]) -> dict[str, Any]:
    """Pure recomputation of the desired {parent, blocked_by, blocking}
    snapshot from an ISSUE_RELATIONSHIP_UPDATE_INPUT_V1 payload's
    expected_before + add/remove sets. Used only for PR #1897 P1-5 receipt-
    loss classification (never for the authoritative accept/reject decision,
    which always belongs to the controlled executor's own live readback)."""
    before = executor_payload.get("expected_before") or {}
    parent_action = executor_payload.get("parent") or {}
    desired_parent = before.get("parent")
    if parent_action.get("action") == "set":
        desired_parent = parent_action.get("issue_number")
    elif parent_action.get("action") == "remove":
        desired_parent = None
    desired_bb = sorted(
        (set(before.get("blocked_by", [])) | set(executor_payload.get("add_blocked_by", [])))
        - set(executor_payload.get("remove_blocked_by", []))
    )
    desired_bl = sorted(
        (set(before.get("blocking", [])) | set(executor_payload.get("add_blocking", [])))
        - set(executor_payload.get("remove_blocking", []))
    )
    return {"parent": desired_parent, "blocked_by": desired_bb, "blocking": desired_bl}


def _prepare_native_relationship(
    state: "TxnState", relationship_input: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Phase A (Required Transaction Order steps 1/2/3/4): native relationship
    capability preflight, pre-readback, expected_before drift check, and pure
    graph-invariant validation of the caller-declared native_relationships
    block. This function never performs a GraphQL mutation -- it only
    decides whether a mutation attempt would be safe.

    Issue #1897 P0-1: the actual GraphQL mutation (_execute_native_relationship)
    must be invoked strictly after the candidate body has already passed
    guard/hygiene/readiness validation. Previously the mutation ran before
    that validation, so a post-mutation content-validation failure was
    misreported as failed_no_mutation even though a real remote mutation had
    already succeeded.

    Returns (ok, payload_or_result). On ok=True, payload_or_result is the
    fully-shaped ISSUE_RELATIONSHIP_UPDATE_INPUT_V1 executor payload
    (raw add/remove sets forwarded exactly as declared by the caller --
    Issue #1897 P1-1: deduplicating/sorting here before validation would
    defeat validate_issue_relationship_update_input()'s explicit rejection
    of malformed duplicate/unsorted input). On ok=False, payload_or_result is
    already a complete native_relationships result block with
    attempted=False, since nothing in this phase ever writes to GitHub.
    """
    cap_ok, cap_err = _relationship_capability_preflight()
    if not cap_ok:
        return False, {
            **DEFAULT_NATIVE_RESULT,
            "status": "failed_no_mutation",
            "errors": [{"code": "relationship_capability_preflight_failed", "message": cap_err}],
        }

    live_before, live_err = _fetch_native_relationships(state.issue_number, state.repo)
    if live_before is None:
        return False, {
            **DEFAULT_NATIVE_RESULT,
            "status": "failed_no_mutation",
            "errors": [{"code": "relationship_pre_readback_failed", "message": live_err}],
        }

    expected_before = relationship_input.get("expected_before")
    if expected_before is None:
        expected_before_normalized = live_before
    else:
        expected_before_normalized = {
            "parent": expected_before.get("parent"),
            "blocked_by": sorted(expected_before.get("blocked_by", [])),
            "blocking": sorted(expected_before.get("blocking", [])),
        }
        if expected_before_normalized != live_before:
            return False, {
                **DEFAULT_NATIVE_RESULT,
                "status": "failed_no_mutation",
                "before": live_before,
                "errors": [
                    {
                        "code": "expected_before_drift_detected",
                        "message": "native_relationships.expected_before does not match live pre-readback",
                    }
                ],
            }

    parent_action = relationship_input.get("parent") or {"action": "unchanged", "issue_number": None}
    add_bb = relationship_input.get("add_blocked_by", [])
    rm_bb = relationship_input.get("remove_blocked_by", [])
    add_bl = relationship_input.get("add_blocking", [])
    rm_bl = relationship_input.get("remove_blocking", [])

    idempotency_key = (
        f"{state.repo}:{state.issue_number}:relationship:"
        f"{parent_action.get('action')}:{parent_action.get('issue_number')}:{add_bb}:{rm_bb}:{add_bl}:{rm_bl}"
    )
    executor_payload = {
        "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
        "issue_number": state.issue_number,
        "repo": state.repo,
        "expected_before": expected_before_normalized,
        "parent": {
            "action": parent_action.get("action", "unchanged"),
            "issue_number": parent_action.get("issue_number"),
        },
        "add_blocked_by": add_bb,
        "remove_blocked_by": rm_bb,
        "add_blocking": add_bl,
        "remove_blocking": rm_bl,
        "idempotency_key": idempotency_key,
    }

    _validate_relationship_graph_invariants = _load_validate_relationship_graph_invariants()
    field_err = _validate_relationship_graph_invariants(executor_payload, state.issue_number, state.repo)
    if field_err:
        return False, {
            **DEFAULT_NATIVE_RESULT,
            "status": "failed_no_mutation",
            "before": expected_before_normalized,
            "errors": [{"code": "graph_invariant_violation", "message": field_err}],
        }

    return True, executor_payload


def _execute_native_relationship(
    state: "TxnState", executor_payload: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Phase B: the only place in this module that actually attempts a
    GitHub-native relationship mutation (Required Transaction Order steps
    5/6/7). Must only be called after _prepare_native_relationship succeeded
    AND the candidate body/title has already passed guard/hygiene/readiness
    validation (Issue #1897 P0-1)."""
    input_ref = _write_issue_metadata_input(state.issue_number, "issue_relationship.update", executor_payload)
    state.relationship_input_ref = input_ref
    cp, result = _invoke_controlled_exec("issue_relationship.update", state.issue_number, state.repo, input_ref)
    parsed = result or {}
    exec_status = parsed.get("status")
    mutation_attempted = bool(parsed.get("mutation_attempted", False))

    if cp.returncode == 0 and exec_status in ("no_op", "applied"):
        return True, {
            "attempted": mutation_attempted,
            "status": exec_status,
            "before": parsed.get("before", executor_payload.get("expected_before")),
            "desired": parsed.get("desired"),
            "after": parsed.get("after", parsed.get("before", executor_payload.get("expected_before"))),
            "completed_operations": parsed.get("completed_operations", []),
            "pending_operations": parsed.get("pending_operations", []),
            "errors": [],
        }

    if cp.returncode == 124 or result is None:
        # Issue #1897 P1-5: a controlled-executor child that timed out or
        # produced non-JSON output after having actually been launched must
        # never be assumed to have made zero remote changes -- the GraphQL
        # mutation may have completed before the child process's result JSON
        # reached this parent. Perform one independent, canonical readback
        # and classify against both the desired and before snapshots instead
        # of guessing "no mutation occurred".
        desired_guess = _compute_desired_relationship_snapshot(executor_payload)
        before_guess = executor_payload.get("expected_before")
        observed, observed_err = _fetch_native_relationships(state.issue_number, state.repo)
        if observed is not None and observed == desired_guess:
            receipt_loss_status = "applied_with_receipt_loss"
        elif observed is not None and observed == before_guess:
            receipt_loss_status = "failed_no_mutation"
        else:
            receipt_loss_status = "failed_after_mutation"
        return False, {
            "attempted": True,
            "status": receipt_loss_status,
            "before": before_guess,
            "desired": desired_guess,
            "after": observed,
            "completed_operations": [],
            "pending_operations": [],
            "errors": [
                _child_error(cp, "issue_relationship_update_child_receipt_lost"),
                *([{"code": "post_receipt_loss_readback_failed", "message": observed_err}] if observed is None else []),
            ],
        }

    overall_status = "failed_after_mutation" if mutation_attempted else "failed_no_mutation"
    return False, {
        "attempted": mutation_attempted,
        "status": overall_status,
        "before": parsed.get("before", executor_payload.get("expected_before")),
        "desired": parsed.get("desired"),
        "after": parsed.get("after"),
        "completed_operations": parsed.get("completed_operations", []),
        "pending_operations": parsed.get("pending_operations", []),
        "errors": [_child_error(cp, "issue_relationship_update_failed")],
    }


def _normalize_relationship_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Order-independent normalization of a {parent, blocked_by, blocking}
    snapshot for exact-set comparison (AC4/AC5): the controlled executor's
    full-pagination readback is order-independent by design."""
    if not isinstance(snapshot, dict):
        return snapshot
    return {
        "parent": snapshot.get("parent"),
        "blocked_by": sorted(snapshot.get("blocked_by", []) or []),
        "blocking": sorted(snapshot.get("blocking", []) or []),
    }


def _finalize_native_relationships(
    state: "TxnState", rel_result: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Phase C (Required Transaction Order steps 9/10; Issue #1897 P0-2):
    after content mutation (or a confirmed content no-op) completes, perform
    one more full, fresh readback of parent/blocked_by/blocking and require
    it to still match the desired snapshot recorded by Phase B. Reuses the
    existing issue_relationship.update controlled-executor command as a
    verify-only, zero-delta probe (expected_before=desired, empty add/
    remove, parent action=unchanged) instead of adding a new command id or
    schema (#1860 minimal-harness policy). A mismatch here means some actor
    changed the native relationship graph after Phase B confirmed it, and
    the transaction must not report `ok`."""
    desired = rel_result.get("desired")
    if not isinstance(desired, dict):
        return True, rel_result

    verify_payload = {
        "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
        "issue_number": state.issue_number,
        "repo": state.repo,
        "expected_before": desired,
        "parent": {"action": "unchanged", "issue_number": None},
        "add_blocked_by": [],
        "remove_blocked_by": [],
        "add_blocking": [],
        "remove_blocking": [],
        "idempotency_key": (
            f"{state.repo}:{state.issue_number}:relationship-verify:"
            f"{desired.get('parent')}:{desired.get('blocked_by')}:{desired.get('blocking')}"
        ),
    }
    input_ref = _write_issue_metadata_input(state.issue_number, "issue_relationship.update", verify_payload)
    cp, result = _invoke_controlled_exec("issue_relationship.update", state.issue_number, state.repo, input_ref)
    parsed = result or {}
    after = parsed.get("after")
    if after is None:
        after = parsed.get("before")

    if (
        cp.returncode == 0
        and parsed.get("status") in ("no_op", "applied")
        and _normalize_relationship_snapshot(after) == _normalize_relationship_snapshot(desired)
    ):
        updated = dict(rel_result)
        updated["after"] = after
        return True, updated

    updated = dict(rel_result)
    updated["status"] = "failed_after_mutation"
    updated["errors"] = list(rel_result.get("errors", [])) + [
        {
            "code": "final_native_relationship_readback_drift",
            "message": "post-content-mutation native relationship readback did not match desired state",
        }
    ]
    return False, updated


def _safe_repo_file(relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError("path_must_be_relative")
    normalized = Path(os.path.normpath(relative_path))
    if not normalized.parts:
        raise ValueError("path_not_found")
    if ".." in normalized.parts:
        raise ValueError("path_must_not_escape_repo")
    repo_root = REPO_ROOT.resolve()
    resolved_cursor = repo_root
    final_lstat = None
    for part in normalized.parts:
        resolved_cursor = resolved_cursor / part
        try:
            st = resolved_cursor.lstat()
        except FileNotFoundError:
            raise ValueError("path_not_found")
        except OSError as exc:
            raise ValueError(f"path_lstat_error: {exc}") from exc
        final_lstat = st
        if resolved_cursor.is_symlink():
            raise ValueError(f"symlink_not_allowed: {resolved_cursor}")
    try:
        resolved = resolved_cursor.resolve()
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("path_must_resolve_under_repo") from exc
    except OSError as exc:
        raise ValueError(f"path_resolve_error: {exc}") from exc
    if final_lstat is None or not resolved.is_file():
        raise ValueError("path_not_file")
    if final_lstat.st_nlink != 1:
        raise ValueError("hardlink_not_allowed")
    try:
        resolved.stat()
    except OSError:
        raise ValueError("path_not_found")
    return resolved


def _parse_controlled_exec_output(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_text_file(relative_path: str) -> str:
    return _safe_repo_file(relative_path).read_text(encoding="utf-8")


def _run_command(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(REPO_ROOT),
            timeout=CONTROLLED_EXEC_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args,
            124,
            stdout="",
            stderr=(
                f"child command timeout after {int(exc.timeout)}s"
                if exc.timeout is not None
                else "child command timeout"
            ),
        )


def _child_error(cp: subprocess.CompletedProcess[str], code: str) -> dict[str, str]:
    detail = cp.stderr.strip() or cp.stdout.strip()
    return {"code": code, "message": _bounded(detail or f"returncode={cp.returncode}", MAX_CHILD_SNIPPET)}


def _hygiene_rejection(hygiene_cp: subprocess.CompletedProcess[str]) -> dict[str, str] | None:
    """Classify the hygiene autofix result; None means a success path.

    exit 0 = repaired (candidate rewritten), exit 1 = no repair needed. exit 2
    (unsafe repair / checker or compiler failure), timeout (124), any other
    exit code, or unexpected
    stdout (the candidate is written via --out-file, stdout stays empty) are
    rejected before any mutation.
    """
    rc = hygiene_cp.returncode
    if rc == 124:
        return _child_error(hygiene_cp, "issue_contract_hygiene_timeout")
    if rc == 2:
        return _child_error(hygiene_cp, "issue_contract_hygiene_unsafe_or_checker_error")
    if rc not in (0, 1):
        return _child_error(hygiene_cp, "issue_contract_hygiene_runtime_error")
    if rc == 1 and "Traceback" in hygiene_cp.stderr:
        # an uncaught Python exception also exits 1; only a clean `status: no_change` is "no repair needed"
        return _child_error(hygiene_cp, "issue_contract_hygiene_runtime_error")
    if hygiene_cp.stdout.strip():
        return {
            "code": "issue_contract_hygiene_invalid_output",
            "message": _bounded(f"returncode={rc} with unexpected stdout (--out-file writes the candidate only)"),
        }
    return None


def _fetch_issue(issue_number: int, repo: str) -> tuple[dict[str, Any] | None, str]:
    gh = shutil.which("gh") or "gh"
    cp = _run_command(
        [gh, "issue", "view", str(issue_number), "--repo", repo, "--json", "title,body,updatedAt,state"],
        env=_sanitized_gh_env(),
    )
    if cp.returncode != 0:
        return None, _bounded(cp.stderr.strip() or cp.stdout.strip())
    try:
        return json.loads(cp.stdout), ""
    except json.JSONDecodeError:
        return None, "gh_issue_view_non_json"


# Issue #2922: pre-existing readiness defect compatibility lane.
# A CLOSED historical Issue whose live body already carries blocking readiness
# defects may still receive a note-only update (`## Notes for Reviewer` only)
# as long as the readiness defect multiset is exactly unchanged. The lane never
# bypasses guard / hygiene / sha / updatedAt / readback checks.
NOTES_FOR_REVIEWER_HEADING = "## Notes for Reviewer"
PREEXISTING_READINESS_CHECKER_EXIT_CODES = frozenset({0, 1})
NATIVE_RELATIONSHIP_LIST_FIELDS = ("add_blocked_by", "remove_blocked_by", "add_blocking", "remove_blocking")


def _native_relationships_is_noop(relationship_input: Any) -> bool:
    if relationship_input is None:
        return True
    if not isinstance(relationship_input, dict):
        return False
    parent = relationship_input.get("parent") or {"action": "unchanged"}
    if not isinstance(parent, dict) or parent.get("action", "unchanged") != "unchanged":
        return False
    return not any(relationship_input.get(field) for field in NATIVE_RELATIONSHIP_LIST_FIELDS)


def _compat_lane_static_preconditions_met(title_update: dict[str, Any], relationship_input: Any) -> bool:
    """Preconditions that need no gh call (title / native relationship scope).

    The live state precondition (CLOSED) is checked separately through a live
    live `state` of the single `_fetch_issue` snapshot, only once this cheap
    check has passed.
    """
    return not title_update.get("required") and _native_relationships_is_noop(relationship_input)


_PROSE_BOUNDARY_POLICY_MODULE_NAME = "_edit_issue_txn_prose_boundary_policy"
_PROSE_BOUNDARY_POLICY_RELPATH = Path(".claude") / "skills" / "create-issue" / "scripts" / "prose_boundary_policy.py"


def _load_prose_boundary_policy() -> Any:
    """Load the shared GFM fence / ATX heading SSOT (prose_boundary_policy.py).

    Resolved from the real on-disk script location (not the mutable REPO_ROOT
    that tests monkeypatch) and loaded lazily under a unique module name, so
    title / body-only calls outside the compatibility lane never depend on it
    and a same-named `prose_boundary_policy` already in `sys.modules` is never
    shadowed or reused.
    """
    cached = sys.modules.get(_PROSE_BOUNDARY_POLICY_MODULE_NAME)
    if cached is not None:
        return cached
    import importlib.util

    module_path = SCRIPT_PATH.parents[4] / _PROSE_BOUNDARY_POLICY_RELPATH
    spec = importlib.util.spec_from_file_location(_PROSE_BOUNDARY_POLICY_MODULE_NAME, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PROSE_BOUNDARY_POLICY_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_PROSE_BOUNDARY_POLICY_MODULE_NAME, None)
        raise
    return module


def _split_h2_sections(body: str) -> list[tuple[str | None, str]]:
    """Split a body into (heading, raw_text) chunks at top-level H2 lines.

    The first chunk (heading None) is the preamble before the first H2. Fenced
    code and H2 detection follow CommonMark / GFM exactly through the shared
    `prose_boundary_policy` SSOT: 0-3 spaces indent fences and ATX headings,
    4+ spaces indent is code, a closer must be the same character and at least
    as long as its opener. Lines inside a fence never start a new section. The
    heading key is `"## <text>"`.
    """
    policy = _load_prose_boundary_policy()
    lines = body.splitlines(keepends=True)
    fenced: set[int] = set()
    cursor = 0
    for block_text, block_kind in policy.iter_markdown_blocks(body):
        block_len = len(block_text.splitlines(keepends=True))
        if block_kind == policy.BLOCK_KIND_CODE_FENCE:
            fenced.update(range(cursor, cursor + block_len))
        cursor += block_len
    if cursor != len(lines):
        raise ValueError("markdown block segmentation did not cover every body line")

    chunks: list[tuple[str | None, list[str]]] = [(None, [])]
    for index, line in enumerate(lines):
        heading = None if index in fenced else policy.parse_atx_heading_line(line.rstrip("\r\n"))
        if heading is not None and heading["level"] == 2:
            chunks.append((f"## {heading['text']}", [line]))
        else:
            chunks[-1][1].append(line)
    return [(heading, "".join(chunk_lines)) for heading, chunk_lines in chunks]


def _non_note_sections(body: str) -> list[tuple[str | None, str]]:
    # Trailing newlines are separator bytes between sections (appending a note
    # section legitimately adds a blank line after the previous section), so
    # they are not part of a section's compared content.
    return [
        (heading, text.rstrip("\r\n"))
        for heading, text in _split_h2_sections(body)
        if heading != NOTES_FOR_REVIEWER_HEADING
    ]


def _only_notes_section_differs(live_body: str, candidate_body: str) -> bool:
    try:
        return _non_note_sections(live_body) == _non_note_sections(candidate_body)
    except (ImportError, ValueError, OSError):
        # Boundary SSOT unavailable (missing / unreadable file) / inconsistent
        # segmentation: fail closed.
        return False


def _defect_multiset_from_checker_output(rc: int, stdout: str) -> Counter | None:
    """Opaque (rule_id, category, section, minimal_context) multiset, or None (fail-closed)."""
    if rc not in PREEXISTING_READINESS_CHECKER_EXIT_CODES:
        return None
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("errors"), list):
        return None
    counter: Counter = Counter()
    for error in data["errors"]:
        if not isinstance(error, dict):
            return None
        key = tuple(
            json.dumps(error.get(field), sort_keys=True)
            for field in ("rule_id", "category", "section", "minimal_context")
        )
        counter[key] += 1
    return counter


def _preexisting_readiness_defects_unchanged(
    live_body: str, mutated_candidate: str, candidate_readiness_cp: subprocess.CompletedProcess[str]
) -> bool:
    """True only when the candidate changes nothing but the notes section and
    the real static readiness checker reports an identical defect multiset for
    the live body and the mutated candidate (fail-closed otherwise)."""
    if not _only_notes_section_differs(live_body, mutated_candidate):
        return False
    candidate_defects = _defect_multiset_from_checker_output(
        candidate_readiness_cp.returncode, candidate_readiness_cp.stdout
    )
    if candidate_defects is None:
        return False
    tmp_dir = REPO_ROOT / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    live_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=str(tmp_dir), encoding="utf-8"
        ) as live_file:
            live_file.write(live_body)
            live_path = Path(live_file.name)
        live_cp = _run_command(
            [sys.executable, str(READINESS_SCRIPT), "--body-file", str(live_path), "--mode", "static"]
        )
    finally:
        if live_path is not None:
            live_path.unlink(missing_ok=True)
    live_defects = _defect_multiset_from_checker_output(live_cp.returncode, live_cp.stdout)
    if live_defects is None:
        return False
    return live_defects == candidate_defects


def _render_result(
    *,
    status: str,
    issue_number: int | None,
    repo: str | None,
    mutation_started: bool,
    body_attempted: bool,
    body_status: str,
    comment_attempted: bool,
    comment_status: str,
    comment_id: str | None,
    comment_url: str | None,
    comment_body_sha256: str | None,
    previous_body_sha256: str | None,
    requested_new_body_sha256: str | None,
    remote_current_body_sha256: str | None,
    body_input_ref: str | None,
    comment_input_ref: str | None,
    errors: list[dict[str, str]],
    previous_title: str | None = None,
    requested_title: str | None = None,
    remote_current_title: str | None = None,
    patch_attempted: bool = False,
    mutation_outcome: str = "not_attempted",
    native_relationships: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Issue #1897 P0-1 bullet 5: once any remote write may have been
    # attempted (native relationship mutation, content update, or comment
    # publish), the transaction must never report failed_no_mutation --
    # doing so would tell a caller "GitHub was not changed" when it might
    # have been.
    nr_dict = dict(native_relationships) if native_relationships else dict(DEFAULT_NATIVE_RESULT)
    if nr_dict.get("attempted"):
        # Issue #1897 P0-1 bullet 5 / P1-5: a native relationship mutation
        # that was actually attempted is itself a real remote write, even if
        # the caller building this particular _render_result call did not
        # thread that fact through mutation_started.
        mutation_started = True
    if status == "failed_no_mutation" and mutation_started:
        status = "failed_after_mutation"
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "mutation_started": mutation_started,
        "rollback_attempted": False,
        "body_update": {
            "attempted": body_attempted,
            "status": body_status,
            "previous_body_sha256": previous_body_sha256,
            "new_body_sha256": requested_new_body_sha256,
            "remote_current_body_sha256": remote_current_body_sha256,
            "artifact_ref": body_input_ref,
        },
        "content_update": {
            "previous_title": previous_title,
            "requested_title": requested_title,
            "remote_current_title": remote_current_title,
            "patch_attempted": patch_attempted,
            "mutation_outcome": mutation_outcome,
        },
        "comment_publish": {
            "attempted": comment_attempted,
            "status": comment_status,
            "comment_id": comment_id,
            "comment_url": comment_url,
            "comment_body_sha256": comment_body_sha256,
            "artifact_ref": comment_input_ref,
        },
        "errors": _truncate_errors(errors),
        "native_relationships": nr_dict,
    }


def _write_issue_metadata_input(issue_number: int, command_id: str, payload: dict[str, Any]) -> str:
    txn_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    relative = Path("artifacts") / str(issue_number) / "issue-metadata" / command_id / f"{txn_id}.input.json"
    absolute = REPO_ROOT / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return str(relative).replace("\\", "/")


def _invoke_controlled_exec(
    command_id: str,
    issue_number: int,
    repo: str,
    input_ref: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    cp = _run_command(
        [
            sys.executable,
            str(CONTROLLED_EXEC),
            "--command-id",
            command_id,
            "--issue-number",
            str(issue_number),
            "--input-file",
            input_ref,
            "--repo",
            repo,
            "--json",
        ]
    )
    return cp, _parse_controlled_exec_output(cp.stdout)


def _extract_comment_publish_result(
    result: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    if not result:
        return None, None, None
    comment_id = result.get("comment_id")
    comment_url = result.get("comment_url")
    comment_body_sha256 = result.get("body_sha256")
    normalized_comment_id = comment_id if isinstance(comment_id, str) and comment_id else None
    normalized_comment_url = comment_url if isinstance(comment_url, str) and comment_url else None
    normalized_comment_body_sha256 = (
        comment_body_sha256 if isinstance(comment_body_sha256, str) and comment_body_sha256 else None
    )
    return normalized_comment_id, normalized_comment_url, normalized_comment_body_sha256


def _run_comment_publish(
    state: TxnState,
    comment_mode: dict[str, Any],
) -> bool:
    if comment_mode.get("mode") != "publish":
        return True

    state.comment_attempted = True
    comment_body = _read_text_file(comment_mode["comment_body_file"])
    marker = comment_mode["marker"]
    if marker not in comment_body:
        state.comment_status = "failed"
        state.errors.append(
            {
                "code": "comment_marker_not_embedded_in_body",
                "message": "comment body must contain marker before executor invocation",
            }
        )
        return False

    comment_input = {
        "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
        "issue_number": state.issue_number,
        "comment_body": comment_body,
        "marker": marker,
    }
    state.comment_input_ref = _write_issue_metadata_input(
        state.issue_number, "issue_comment.publish", comment_input
    )
    comment_cp, comment_result = _invoke_controlled_exec(
        "issue_comment.publish", state.issue_number, state.repo, state.comment_input_ref
    )
    state.comment_id, state.comment_url, state.comment_body_sha256 = _extract_comment_publish_result(comment_result)
    if comment_cp.returncode != 0:
        state.comment_status = "failed"
        state.errors.append(_child_error(comment_cp, "issue_comment_publish_failed"))
        return False

    state.comment_status = "ok"
    return True


@dataclass
class TxnState:
    issue_number: int
    repo: str
    previous_body_sha256: str | None = None
    requested_new_body_sha256: str | None = None
    remote_current_body_sha256: str | None = None
    mutation_started: bool = False
    body_attempted: bool = False
    body_status: str = "not_run"
    comment_attempted: bool = False
    comment_status: str = "not_run"
    comment_id: str | None = None
    comment_url: str | None = None
    comment_body_sha256: str | None = None
    body_input_ref: str | None = None
    comment_input_ref: str | None = None
    relationship_input_ref: str | None = None
    errors: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def run_transaction(input_data: dict[str, Any]) -> dict[str, Any]:
    _validate_input_payload(input_data)
    state = TxnState(issue_number=input_data["issue_number"], repo=input_data["repo"])
    title_update = input_data.get("title_update") or {"required": False}
    readiness_result = input_data["readiness_forwarding_payload"]["readiness_result"]
    forwarded_status = readiness_result["status"]
    if forwarded_status in {"human_judgment", "input_or_runtime_error"}:
        state.errors.append(
            {
                "code": "readiness_forwarding_requires_human_judgment",
                "message": f"forwarded readiness status={forwarded_status}",
            }
        )
        return _render_result(
            status="human_judgment",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    # Issue #2922: the forwarded needs_fix (no resolution_evidence) rejection is
    # deferred until after the live readback so the pre-existing readiness
    # compatibility lane can evaluate the live state. Any unmet lane condition
    # reproduces the original immediate rejection (same code, no child
    # process, no mutation).
    deferred_needs_fix = forwarded_status == "needs_fix" and not readiness_result.get("resolution_evidence")

    def _reject_needs_fix_without_resolution_evidence() -> dict[str, Any]:
        state.errors.append(
            {
                "code": "readiness_needs_fix_without_resolution_evidence",
                "message": "forwarded readiness status=needs_fix without resolution_evidence",
            }
        )
        return _render_result(
            status="failed_no_mutation",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    # The CLOSED precondition is the live `state` of the single
    # `gh issue view --json title,body,updatedAt,state` readback taken below
    # (never caller-declared, and never a separate request). A missing /
    # non-str state, or a failed readback, is treated as "not CLOSED"
    # (fail-closed, original rejection).
    compat_lane_static_ok = _compat_lane_static_preconditions_met(title_update, input_data.get("native_relationships"))
    live_state: str | None = None

    def _live_state_is_closed() -> bool:
        return live_state == "CLOSED"

    if deferred_needs_fix and not compat_lane_static_ok:
        return _reject_needs_fix_without_resolution_evidence()
    issue_data, issue_error = _fetch_issue(state.issue_number, state.repo)
    if isinstance(issue_data, dict) and isinstance(issue_data.get("state"), str):
        live_state = issue_data["state"]
    if deferred_needs_fix and (issue_data is None or not _live_state_is_closed()):
        return _reject_needs_fix_without_resolution_evidence()
    if issue_data is None:
        state.errors.append({"code": "issue_readback_failed", "message": issue_error})
        return _render_result(
            status="failed_no_mutation",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    current_title = issue_data.get("title", "")
    current_body = issue_data.get("body", "")
    current_updated_at = issue_data.get("updatedAt", "")
    current_sha = _sha256_text(current_body)
    state.previous_body_sha256 = current_sha
    state.remote_current_body_sha256 = current_sha
    requested_title = title_update.get("proposed_title") if title_update.get("required") else current_title
    operation_reason = title_update.get("reason") if title_update.get("required") else "issue_body_update"
    if (
        current_sha != input_data["expected_previous_body_sha256"]
        or current_updated_at != input_data["expected_previous_updated_at"]
    ):
        state.errors.append(
            {
                "code": "stale_precondition_before_mutation",
                "message": "remote body sha or updatedAt changed before controlled executor invocation",
            }
        )
        return _render_result(
            status="failed_no_mutation",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=current_sha,
            requested_new_body_sha256=None,
            remote_current_body_sha256=current_sha,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    # Issue #1897 P0-1: native relationship handling is split into three
    # phases. Phase A (pure validation + read-only preflight) runs here,
    # before anything is read/mutated for content. Phase B (the actual
    # GraphQL mutation) is deferred until *after* the candidate body has
    # passed guard/hygiene/readiness validation below -- previously Phase B
    # ran first, so a post-mutation content-validation failure was
    # misreported as failed_no_mutation even though a real remote mutation
    # had already succeeded. Phase C (final combined readback) runs right
    # before every `ok`/`no_change` return. native_relationships is an
    # additive, optional ISSUE_EDIT_TXN_INPUT_V1 field; omitting it preserves
    # pre-#1883 transaction behaviour exactly.
    relationship_input = input_data.get("native_relationships")
    relationship_prepared: dict[str, Any] | None = None
    rel_result_for_output: dict[str, Any] | None = None
    if relationship_input is not None:
        prep_ok, prepared_or_result = _prepare_native_relationship(state, relationship_input)
        if not prep_ok:
            rel_result_for_output = prepared_or_result
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_no_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=bool(rel_result_for_output.get("attempted")),
                body_attempted=False,
                body_status="not_run",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=None,
                remote_current_body_sha256=current_sha,
                body_input_ref=None,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=list(state.errors) + list(rel_result_for_output.get("errors", [])),
            )
        relationship_prepared = prepared_or_result

    # Issue #1897 P0-1 bullet 1: the candidate body (and, later, comment
    # body) are always read before any mutation -- native or content -- is
    # attempted.
    new_body = _read_text_file(input_data["new_body_file"])
    body_change_requested = new_body != current_body
    comment_mode = input_data.get("comment_mode", {"mode": "skip"})

    candidate_path: Path | None = None
    try:
        if body_change_requested:
            tmp_dir = REPO_ROOT / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".md",
                delete=False,
                dir=str(tmp_dir),
                encoding="utf-8",
            ) as tmp_body:
                tmp_body.write(new_body)
                candidate_path = Path(tmp_body.name)

            guard_cp = _run_command([sys.executable, str(GUARD_SCRIPT), str(candidate_path), "--format", "json"])
            if guard_cp.returncode != 0:
                state.errors.append(_child_error(guard_cp, "guard_or_readiness_failed_before_mutation"))
                return _render_result(
                    native_relationships=rel_result_for_output,
                    status="failed_no_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=False, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=_sha256_text(new_body), remote_current_body_sha256=current_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
            hygiene_cp = _run_command(
                [
                    sys.executable,
                    str(HYGIENE_SCRIPT),
                    "--body-file",
                    str(candidate_path),
                    "--out-file",
                    str(candidate_path),
                ]
            )
            hygiene_error = _hygiene_rejection(hygiene_cp)
            if hygiene_error is not None:
                state.errors.append(hygiene_error)
                return _render_result(
                    native_relationships=rel_result_for_output,
                    status="failed_no_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=False, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=_sha256_text(new_body), remote_current_body_sha256=current_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
            mutated_candidate = candidate_path.read_text(encoding="utf-8")
            readiness_cp = _run_command(
                [sys.executable, str(READINESS_SCRIPT), "--body-file", str(candidate_path), "--mode", "static"]
            )
            # Issue #2922: forwarded needs_fix, or a failing candidate static
            # readiness, is only tolerated when the pre-existing readiness
            # compatibility lane proves the defect multiset is unchanged.
            if deferred_needs_fix or readiness_cp.returncode != 0:
                compat_lane_ok = (
                    compat_lane_static_ok
                    and _live_state_is_closed()
                    and _preexisting_readiness_defects_unchanged(current_body, mutated_candidate, readiness_cp)
                )
                if not compat_lane_ok and deferred_needs_fix:
                    state.errors.append(
                        {
                            "code": "readiness_needs_fix_without_resolution_evidence",
                            "message": "forwarded readiness status=needs_fix without resolution_evidence",
                        }
                    )
                elif not compat_lane_ok:
                    state.errors.append(_child_error(readiness_cp, "guard_or_readiness_failed_before_mutation"))
            else:
                compat_lane_ok = True
            if not compat_lane_ok:
                return _render_result(
                    native_relationships=rel_result_for_output,
                    status="failed_no_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=False, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=_sha256_text(mutated_candidate), remote_current_body_sha256=current_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
        else:
            # A title-only request must preserve even a noncanonical body exactly.
            mutated_candidate = current_body

        requested_new_sha = _sha256_text(mutated_candidate)
        state.requested_new_body_sha256 = requested_new_sha

        # Issue #1897 P0-1: Phase B -- the actual native relationship
        # mutation -- only starts here, strictly after the candidate body
        # above has passed guard/hygiene/readiness.
        if relationship_input is not None:
            rel_ok, rel_result_for_output = _execute_native_relationship(state, relationship_prepared)
            if not rel_ok:
                attempted = bool(rel_result_for_output.get("attempted"))
                return _render_result(
                    native_relationships=rel_result_for_output,
                    status="failed_after_mutation" if attempted else "failed_no_mutation",
                    issue_number=state.issue_number, repo=state.repo,
                    mutation_started=attempted, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha, remote_current_body_sha256=current_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None,
                    errors=list(state.errors) + list(rel_result_for_output.get("errors", [])),
                )

            # Issue #1897 P1-7: the native relationship mutation may itself
            # have changed the issue's updatedAt. Re-read title/body/
            # updatedAt now so the content-lane precondition below uses a
            # fresh updatedAt, and fail closed if title/body drifted
            # concurrently with the relationship mutation.
            refreshed_issue, refreshed_err = _fetch_issue(state.issue_number, state.repo)
            if refreshed_issue is None:
                state.errors.append({"code": "post_relationship_readback_failed", "message": refreshed_err})
                return _render_result(
                    native_relationships=rel_result_for_output,
                    status="failed_after_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=True, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha, remote_current_body_sha256=None,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
            refreshed_sha = _sha256_text(refreshed_issue.get("body", ""))
            refreshed_title = refreshed_issue.get("title", "")
            if refreshed_sha != current_sha or refreshed_title != current_title:
                state.errors.append(
                    {
                        "code": "concurrent_content_drift_after_relationship_mutation",
                        "message": "issue title/body changed concurrently with native relationship mutation",
                    }
                )
                return _render_result(
                    native_relationships=rel_result_for_output,
                    status="failed_after_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=True, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha, remote_current_body_sha256=refreshed_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
            current_updated_at = refreshed_issue.get("updatedAt", current_updated_at)

        is_no_change = requested_new_sha == current_sha and requested_title == current_title
        if is_no_change:
            if comment_mode.get("mode") == "publish":
                if not _run_comment_publish(state, comment_mode):
                    return _render_result(
                        native_relationships=rel_result_for_output,
                        status="failed_after_mutation",
                        issue_number=state.issue_number,
                        repo=state.repo,
                        mutation_started=state.comment_attempted,
                        body_attempted=False,
                        body_status="not_run",
                        comment_attempted=state.comment_attempted,
                        comment_status=state.comment_status,
                        previous_body_sha256=current_sha,
                        requested_new_body_sha256=requested_new_sha,
                        remote_current_body_sha256=current_sha,
                        body_input_ref=None,
                        comment_input_ref=state.comment_input_ref,
                        comment_id=state.comment_id,
                        comment_url=state.comment_url,
                        comment_body_sha256=state.comment_body_sha256,
                        errors=state.errors,
                    )
                result = _render_result(
                    native_relationships=rel_result_for_output,
                    status="ok",
                    issue_number=state.issue_number,
                    repo=state.repo,
                    mutation_started=True,
                    body_attempted=False,
                    body_status="not_run",
                    comment_attempted=True,
                    comment_status="ok",
                    previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha,
                    remote_current_body_sha256=current_sha,
                    body_input_ref=None,
                    comment_input_ref=state.comment_input_ref,
                    comment_id=state.comment_id,
                    comment_url=state.comment_url,
                    comment_body_sha256=state.comment_body_sha256,
                    errors=[],
                )
            else:
                # Issue #1897 P1-5: a native relationship mutation that
                # actually applied is itself a real remote mutation, even
                # when title/body are unchanged -- the top-level status must
                # never say `no_change` in that case.
                native_applied = bool(
                    relationship_input is not None
                    and rel_result_for_output
                    and rel_result_for_output.get("attempted")
                )
                result = _render_result(
                    native_relationships=rel_result_for_output,
                    status="ok" if native_applied else "no_change",
                    issue_number=state.issue_number,
                    repo=state.repo,
                    mutation_started=native_applied,
                    body_attempted=False,
                    body_status="not_run",
                    comment_attempted=False,
                    comment_status="not_run",
                    previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha,
                    remote_current_body_sha256=current_sha,
                    body_input_ref=None,
                    comment_input_ref=None,
                    comment_id=None,
                    comment_url=None,
                    comment_body_sha256=None,
                    errors=[],
                    previous_title=current_title,
                    requested_title=requested_title,
                    remote_current_title=current_title,
                    patch_attempted=False,
                    mutation_outcome="no_change",
                )
            if relationship_input is not None:
                verify_ok, rel_result_for_output = _finalize_native_relationships(state, rel_result_for_output)
                result["native_relationships"] = rel_result_for_output
                if not verify_ok:
                    result["status"] = "failed_after_mutation"
                    result["mutation_started"] = True
                    result["errors"] = _truncate_errors(
                        list(state.errors) + list(rel_result_for_output.get("errors", []))
                    )
            return result

        body_input = {
            "schema": "ISSUE_CONTENT_UPDATE_INPUT_V1",
            "issue_number": state.issue_number,
            "repo": state.repo,
            "expected_previous_title": current_title,
            "expected_previous_body_sha256": current_sha,
            "expected_previous_updated_at": current_updated_at,
            "new_title": requested_title,
            "new_body": mutated_candidate,
            "new_body_sha256": requested_new_sha,
            "operation_reason": operation_reason,
            "idempotency_key": f"{state.repo}:{state.issue_number}:{current_sha}:{requested_new_sha}:{requested_title}",
        }
        state.body_input_ref = _write_issue_metadata_input(state.issue_number, "issue_content.update", body_input)
        state.body_attempted = True
        body_cp, body_result = _invoke_controlled_exec(
            "issue_content.update", state.issue_number, state.repo, state.body_input_ref
        )
        if body_cp.returncode != 0:
            refreshed_issue, _ = _fetch_issue(state.issue_number, state.repo)
            refreshed_body = (refreshed_issue or {}).get("body", current_body)
            refreshed_title = (refreshed_issue or {}).get("title", current_title)
            refreshed_sha = _sha256_text(refreshed_body)
            state.remote_current_body_sha256 = refreshed_sha
            state.body_status = "failed"
            state.errors.append(_child_error(body_cp, "issue_body_update_failed"))
            if refreshed_sha == requested_new_sha and refreshed_title == requested_title:
                state.mutation_started = True
                return _render_result(
                    native_relationships=rel_result_for_output,
                    status="failed_after_mutation",
                    issue_number=state.issue_number,
                    repo=state.repo,
                    mutation_started=True,
                    body_attempted=True,
                    body_status="failed",
                    comment_attempted=False,
                    comment_status="not_run",
                    previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha,
                    remote_current_body_sha256=refreshed_sha,
                    body_input_ref=state.body_input_ref,
                    comment_input_ref=None,
                    comment_id=None,
                    comment_url=None,
                    comment_body_sha256=None,
                    errors=state.errors,
                )
            return _render_result(
                native_relationships=rel_result_for_output,
                status="mutation_outcome_unknown",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=False,
                body_attempted=True,
                body_status="failed",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=requested_new_sha,
                remote_current_body_sha256=refreshed_sha,
                body_input_ref=state.body_input_ref,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=state.errors,
                previous_title=current_title,
                requested_title=requested_title,
                remote_current_title=refreshed_title if isinstance(refreshed_title, str) else None,
                patch_attempted=True,
                mutation_outcome="unknown",
            )

        state.mutation_started = True
        state.body_status = "ok"
        body_result_sha = requested_new_sha
        if body_result is not None:
            parsed_sha = body_result.get("new_body_sha256")
            if isinstance(parsed_sha, str) and parsed_sha:
                body_result_sha = parsed_sha
                state.requested_new_body_sha256 = parsed_sha

        final_issue, final_error = _fetch_issue(state.issue_number, state.repo)
        if final_issue is None:
            state.errors.append({"code": "final_readback_failed", "message": final_error})
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_after_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=True,
                body_attempted=True,
                body_status="ok",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=body_result_sha,
                remote_current_body_sha256=None,
                body_input_ref=state.body_input_ref,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=state.errors,
            )

        final_sha = _sha256_text(final_issue.get("body", ""))
        final_title = final_issue.get("title", "")
        state.remote_current_body_sha256 = final_sha
        if final_sha != requested_new_sha or final_title != requested_title:
            state.errors.append(
                {
                    "code": "final_readback_content_mismatch",
                    "message": (
                        "controlled content update completed but final readback "
                        "did not match requested title/body"
                    ),
                }
            )
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_after_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=True,
                body_attempted=True,
                body_status="ok",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=body_result_sha,
                remote_current_body_sha256=final_sha,
                body_input_ref=state.body_input_ref,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=state.errors,
            )

        if comment_mode.get("mode") == "publish" and not _run_comment_publish(state, comment_mode):
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_after_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=True,
                body_attempted=True,
                body_status="ok",
                comment_attempted=state.comment_attempted,
                comment_status=state.comment_status,
                previous_body_sha256=current_sha,
                requested_new_body_sha256=body_result_sha,
                remote_current_body_sha256=final_sha,
                body_input_ref=state.body_input_ref,
                comment_input_ref=state.comment_input_ref,
                comment_id=state.comment_id,
                comment_url=state.comment_url,
                comment_body_sha256=state.comment_body_sha256,
                errors=state.errors,
            )

        ok_result = _render_result(
            native_relationships=rel_result_for_output,
            status="ok",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=True,
            body_attempted=True,
            body_status=state.body_status,
            comment_attempted=state.comment_attempted,
            comment_status=state.comment_status,
            previous_body_sha256=current_sha,
            requested_new_body_sha256=body_result_sha,
            remote_current_body_sha256=state.remote_current_body_sha256,
            body_input_ref=state.body_input_ref,
            comment_input_ref=state.comment_input_ref,
            comment_id=state.comment_id,
            comment_url=state.comment_url,
            comment_body_sha256=state.comment_body_sha256,
            errors=[],
            previous_title=current_title,
            requested_title=requested_title,
            remote_current_title=final_title if isinstance(final_title, str) else None,
            patch_attempted=True,
            mutation_outcome="applied",
        )
        if relationship_input is not None:
            verify_ok, rel_result_for_output = _finalize_native_relationships(state, rel_result_for_output)
            ok_result["native_relationships"] = rel_result_for_output
            if not verify_ok:
                ok_result["status"] = "failed_after_mutation"
                ok_result["errors"] = _truncate_errors(
                    list(state.errors) + list(rel_result_for_output.get("errors", []))
                )
        return ok_result
    except Exception as exc:
        # Issue #1897 P0-1 bullet 5: an unexpected exception anywhere after
        # the native relationship mutation (or the content mutation) may
        # have been attempted must never be reported as failed_no_mutation.
        # main()'s own top-level exception handler unconditionally reports
        # mutation_started=False, which would violate that invariant for any
        # exception raised inside this transaction body after a real
        # mutation attempt -- so this transaction-scoped handler reports the
        # correct attempted/status pair using the TxnState accumulated so
        # far instead of letting the exception escape to that generic
        # handler.
        attempted_any = bool(state.mutation_started) or bool(
            rel_result_for_output and rel_result_for_output.get("attempted")
        )
        state.errors.append({"code": "txn_runtime_error_after_validation", "message": str(exc)})
        return _render_result(
            native_relationships=rel_result_for_output,
            status="failed_after_mutation" if attempted_any else "failed_no_mutation",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=attempted_any,
            body_attempted=state.body_attempted,
            body_status=state.body_status,
            comment_attempted=state.comment_attempted,
            comment_status=state.comment_status,
            previous_body_sha256=current_sha,
            requested_new_body_sha256=state.requested_new_body_sha256,
            remote_current_body_sha256=state.remote_current_body_sha256,
            body_input_ref=state.body_input_ref,
            comment_input_ref=state.comment_input_ref,
            comment_id=state.comment_id,
            comment_url=state.comment_url,
            comment_body_sha256=state.comment_body_sha256,
            errors=state.errors,
        )
    finally:
        if candidate_path is not None:
            try:
                candidate_path.unlink()
            except OSError:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run issue edit transaction helper")
    parser.add_argument("--input-file", required=True, help="repo-relative ISSUE_EDIT_TXN_INPUT_V1 JSON path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        input_text = _read_text_file(args.input_file)
        input_data = json.loads(input_text)
        if not isinstance(input_data, dict):
            raise ValueError("input_json_must_be_object")
        result = run_transaction(input_data)
        exit_code = 0 if result["status"] in {"ok", "no_change"} else 1
    except Exception as exc:
        result = _render_result(
            status="failed_no_mutation",
            issue_number=None,
            repo=None,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=[{"code": "txn_input_or_runtime_error", "message": str(exc)}],
        )
        exit_code = 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
