#!/usr/bin/env python3
"""Classify VC failures into VC_ADJUDICATION_RESULT_V1."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_NAME = "VC_ADJUDICATION_RESULT_V1"
SCHEMA_VERSION = 1
TEST_VERDICT_SCHEMA = "TEST_VERDICT_MACHINE/v2"
CURRENT_VC_RESULT_SCHEMA = "baseline_vc_preflight/v1"
PRIVATE_BUNDLE_SCHEMA = "VC_ADJUDICATION_PRIVATE_BUNDLE_V1"
PRIVATE_ARTIFACT_REF = "vc-adjudication-private-bundle"
STATUS_PRIORITY = {
    "pass": 0,
    "pre_existing_fail": 1,
    "out_of_scope_fail": 2,
    "regression_fail": 3,
    "environment_blocked": 4,
    "indeterminate": 5,
}
PATH_RELEVANCE_KINDS = {"pytest_nodeid", "repo_path"}
ENVIRONMENT_BLOCKED_CATEGORIES = {
    "runtime_dependency_error",
    "package_manager_no_tty_prompt",
    "timeout",
}

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ALLOWED_PATHS_GATE_PATH = (
    _REPO_ROOT
    / ".claude"
    / "skills"
    / "pr-review-judge"
    / "scripts"
    / "allowed_paths_review_gate.py"
)
_ALLOWED_PATHS_MATCHER = None

# Issue #1648: receipt-aware verification of Child A
# (TEST_VERDICT_PRODUCER_RECEIPT_V1) provenance embedded in a
# TEST_VERDICT_MACHINE/v2 input. Only consulted when the caller opts in via
# require_producer_receipt=True (adjudicate_vc_result()) /
# --require-producer-receipt (CLI) -- the legacy self-attested TEST_VERDICT
# path (no producer_receipt field checked) is left unchanged so existing
# non-regression fixtures keep passing (Issue #1648 body).
_PRODUCER_RECEIPT_SCHEMA_PATH = _REPO_ROOT / "schemas" / "test-verdict-producer-receipt.schema.json"
_PRODUCER_RECEIPT_VALIDATOR = None


def _get_producer_receipt_validator():
    global _PRODUCER_RECEIPT_VALIDATOR
    if _PRODUCER_RECEIPT_VALIDATOR is not None:
        return _PRODUCER_RECEIPT_VALIDATOR, None
    try:
        from jsonschema import Draft202012Validator
    except Exception as exc:  # pragma: no cover
        return None, f"jsonschema_unavailable:{type(exc).__name__}"
    try:
        schema = json.loads(_PRODUCER_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"producer_receipt_schema_unreadable:{type(exc).__name__}"
    _PRODUCER_RECEIPT_VALIDATOR = Draft202012Validator(schema)
    return _PRODUCER_RECEIPT_VALIDATOR, None


# --- Issue #88 fix_delta Blocker 1: canonical adapter ----------------------
#
# adjudicate_vc_result() accepts a current_vc_result payload in the
# "baseline_vc_preflight/v1" schema, but the read-only report returned by
# the test-runner SubAgent uses a different shape (TEST_VERDICT_MACHINE/v2,
# see .claude/agents/test-runner.md "TEST_VERDICT 報告フォーマット"). Without
# this adapter the two schemas were never actually wired together in the
# live orchestration path, so evaluate_step4_vc_gate() was unreachable from
# a real test-runner report. This performs a structural, non-judgmental
# conversion only: it does not re-classify PASS/FAIL/SKIP and does not
# invent failure_keys. adjudicate_vc_result() remains the sole place VC
# failures are classified against the baseline contract snapshot.
#
# Known scope limitation: TEST_VERDICT.runtime_ac_results[] does not carry
# structured failure_keys (only free-text notes), so the adapted
# results[].failure_keys is always []. This does not affect PASS
# adjudication (Issue #88's primary concern).

_ADAPT_RESULT_STATUS_MAP = {"PASS": "pass", "FAIL": "fail", "PARTIAL": "partial"}


def adapt_test_verdict_to_current_vc_result(test_verdict: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Convert a TEST_VERDICT_MACHINE/v2 payload into a
    "baseline_vc_preflight/v1" payload suitable for adjudicate_vc_result()'s
    current_vc_result argument (or --current-vc-result-file). Returns
    (converted_payload, errors); converted_payload is None only when the
    input cannot be interpreted as TEST_VERDICT_MACHINE/v2 at all.
    """
    payload = test_verdict
    if not isinstance(payload, dict):
        return None, ["test_verdict_not_object"]
    if isinstance(payload.get("TEST_VERDICT"), dict):
        payload = payload["TEST_VERDICT"]
    if payload.get("schema") != TEST_VERDICT_SCHEMA:
        return None, [f"unsupported_source_schema:{payload.get('schema')!r}"]

    errors: list[str] = []
    head_sha = payload.get("head_sha")
    reviewed_head_sha = payload.get("reviewed_head_sha") or head_sha
    contract_body_sha256 = payload.get("contract_body_sha256")
    if not isinstance(head_sha, str) or not head_sha:
        errors.append("missing_head_sha")
    if not isinstance(contract_body_sha256, str) or not contract_body_sha256:
        errors.append("missing_contract_body_sha256")

    runtime_ac_results = payload.get("runtime_ac_results")
    if not isinstance(runtime_ac_results, list):
        errors.append("missing_runtime_ac_results")
        runtime_ac_results = []

    results: list[dict[str, Any]] = []
    any_fallback = False
    any_human_review = bool(payload.get("human_review_required"))
    any_stop_condition = False
    for idx, item in enumerate(runtime_ac_results):
        if not isinstance(item, dict):
            errors.append(f"runtime_ac_results[{idx}]:not_object")
            continue
        ac = item.get("ac")
        command_hash = item.get("command_hash")
        if not isinstance(ac, str) or not ac:
            errors.append(f"runtime_ac_results[{idx}]:missing_ac")
            continue
        if not isinstance(command_hash, str) or not command_hash:
            errors.append(f"runtime_ac_results[{idx}]:missing_command_hash")
            continue
        fallback_detected = bool(item.get("fallback_detected"))
        human_review_required = bool(item.get("human_review_required"))
        stop_condition_triggered = bool(item.get("stop_condition_triggered"))
        any_fallback = any_fallback or fallback_detected
        any_human_review = any_human_review or human_review_required
        any_stop_condition = any_stop_condition or stop_condition_triggered
        results.append(
            {
                "ac": ac,
                "command_hash": command_hash,
                "raw_command": item.get("command"),
                "exit_code": item.get("exit_code"),
                # Issue #2467 P0-1/P1 review fix: carry the per-command
                # execution facts through losslessly so
                # adjudicate_vc_result() can require an ACTUAL executed PASS
                # for a runtime_only (ac, command_hash) on the current side
                # instead of re-requiring the baseline producer-skip
                # envelope (see _is_runtime_only_current_execution_pass()).
                "status": item.get("status"),
                "fallback_detected": fallback_detected,
                "human_review_required": human_review_required,
                "stop_condition_triggered": stop_condition_triggered,
                "failure_keys": [],
                "raw_stdout": "",
                "raw_stderr": item.get("notes") or "",
            }
        )

    converted = {
        "schema": CURRENT_VC_RESULT_SCHEMA,
        "generated_at": payload.get("generated_at"),
        "status": _ADAPT_RESULT_STATUS_MAP.get(payload.get("result"), "indeterminate"),
        "errors": [],
        "fallback_detected": any_fallback,
        "human_review_required": any_human_review,
        "stop_condition_triggered": any_stop_condition,
        "source": {"body_sha256": contract_body_sha256},
        "results": results,
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        # Issue #2467 P1 review fix: preserve payload-level identity fields
        # losslessly so a caller can bind runtime_only current-head evidence
        # to the exact live Issue / PR / diff head by value, not merely by
        # positive-integer presence.
        "issue": payload.get("issue_number"),
        "pr_number": payload.get("pr_number"),
        "diff_head_sha": payload.get("diff_head_sha"),
    }
    return converted, errors
# --- end Issue #88 fix_delta Blocker 1 canonical adapter --------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_command(command: str) -> str:
    return _sha256(command)


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _normalize_command_hash(value: Any, raw_command: Any) -> tuple[str | None, str | None]:
    if isinstance(value, str):
        if value.startswith("sha256:") and _is_hex_64(value[7:]):
            return value, None
        if _is_hex_64(value):
            return "sha256:" + value, "normalized_legacy_bare_command_hash"
    if isinstance(raw_command, str):
        return _sha256_command(raw_command), "derived_command_hash_from_raw_command"
    return None, "missing_or_invalid_command_hash"


def _load_json_file(path: str | None) -> tuple[Any, list[str]]:
    if not path:
        return None, ["missing_input_file"]
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [f"input_file_not_found:{path}"]
    except OSError as exc:
        return None, [f"input_read_error:{path}:{type(exc).__name__}"]
    except UnicodeDecodeError as exc:
        # 非 UTF-8 バイト列。未処理例外にせず構造化拒否として persist・失効経路へ流す。
        return None, [f"input_read_error:{path}:{type(exc).__name__}"]

    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        return None, [f"input_json_error:{path}:{exc}"]
    except (RecursionError, ValueError) as exc:
        # 過大ネスト (RecursionError) / 過大な整数リテラル (ValueError: 4300 桁超) は
        # 未処理例外にせず、例外型名のみを持つ構造化エラーとして返す（成功へは変換しない）。
        return None, [f"input_json_error:{path}:{type(exc).__name__}"]


def _normalize_list_payload(payload: Any) -> tuple[list[Any], list[str], str | None]:
    if payload is None:
        return [], ["input_missing"], None
    if isinstance(payload, list):
        return payload, [], None
    if not isinstance(payload, dict):
        return [], ["input_not_object"], None

    schema = payload.get("schema")
    if schema == "baseline_vc_preflight/v1":
        results = payload.get("results")
        if isinstance(results, list):
            return results, [], schema
        return [], ["missing_baseline_results"], schema

    if schema == "CONTRACT_REVIEW_RESULT_V1":
        checks = payload.get("checks")
        if not isinstance(checks, dict):
            return [], ["missing_checks"], schema
        vc_preflight = checks.get("vc_preflight")
        if isinstance(vc_preflight, dict):
            classifications = vc_preflight.get("classifications")
            if isinstance(classifications, list):
                return classifications, [], schema
        checks_classifications = checks.get("vc_preflight_classifications")
        if isinstance(checks_classifications, list):
            return checks_classifications, [], schema
        return [], ["missing_vc_preflight_classifications"], schema

    if schema == "CONTRACT_REVIEW_ONCE_RESULT_V1":
        results = payload.get("vc_preflight_classifications")
        if isinstance(results, list):
            return results, [], schema
        return [], ["missing_vc_preflight_classifications"], schema

    return [], [f"unsupported_schema:{schema}"], schema


def _normalize_failure_keys(value: Any) -> tuple[list[dict[str, str]], bool]:
    if value is None:
        return [], False
    if isinstance(value, str):
        return [{"kind": "unknown", "key": value}], True
    if not isinstance(value, list):
        return [], False

    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            normalized.append({"kind": "unknown", "key": item})
            continue
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        kind = item.get("kind")
        normalized.append(
            {
                "kind": kind if isinstance(kind, str) and kind else "unknown",
                "key": key,
            }
        )
    return normalized, bool(normalized)


def _normalize_item(item: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(item, dict):
        return None, ["non_object_item"]

    ac = item.get("ac")
    if not isinstance(ac, str) or not ac:
        return None, ["missing_or_invalid_ac"]

    command_hash, command_hash_note = _normalize_command_hash(
        item.get("command_hash"),
        item.get("raw_command"),
    )
    if command_hash is None:
        return None, ["missing_or_invalid_command_hash"]

    exit_code = item.get("exit_code")
    if isinstance(exit_code, bool) or (exit_code is not None and not isinstance(exit_code, int)):
        return None, ["invalid_exit_code"]

    category = item.get("category")
    if category is not None and not isinstance(category, str):
        return None, ["invalid_category"]

    failure_keys, failure_keys_present = _normalize_failure_keys(item.get("failure_keys"))
    normalized = {
        "ac": ac,
        "command_hash": command_hash,
        "exit_code": exit_code,
        "category": category,
        "failure_keys": failure_keys,
        "failure_keys_present": failure_keys_present,
        "classification": item.get("classification"),
        "decision": item.get("decision"),
        "scope_class": item.get("scope_class"),
        "runner": item.get("runner"),
        "verification_owner": item.get("verification_owner"),
        "deferred_reason": item.get("deferred_reason"),
        "runtime_verification_required": item.get("runtime_verification_required"),
        # Issue #2467 P0-1: per-command execution facts. Populated by
        # adapt_test_verdict_to_current_vc_result() for a real test-runner
        # run; None/absent for a baseline_vc_preflight/v1 producer-skip
        # envelope item.
        "status": item.get("status"),
        "fallback_detected": item.get("fallback_detected"),
        "human_review_required": item.get("human_review_required"),
        "stop_condition_triggered": item.get("stop_condition_triggered"),
    }
    if command_hash_note is not None:
        normalized["command_hash_note"] = command_hash_note
    return normalized, []


def _is_producer_authorized_pr_review_only_skip(item: dict[str, Any]) -> bool:
    """Recognize only a complete PR-review-only skip produced by the VC producer."""
    return (
        item.get("runner") == "skipped"
        and item.get("scope_class") == "pr_review_only"
        and item.get("classification") == "skipped"
        and item.get("decision") == "go"
        and item.get("category") == "preflight_scope_pr_review_only"
        and item.get("verification_owner") == "pr-review-judge"
        and isinstance(item.get("deferred_reason"), str)
        and bool(item["deferred_reason"])
        and item.get("runtime_verification_required") is False
    )


# Issue #2467: exact canonical runtime_only producer-skip envelope. This is
# a distinct fail-closed recognizer from _is_producer_authorized_pr_review_only_skip
# above -- runtime_only is NOT an extension of the pr_review_only authorized
# scope (Issue #2467 Out of Scope / #1540 precedent). Only the literal
# envelope emitted by baseline_vc_preflight.py for a `# preflight-scope:
# runtime_only` marker is recognized here; this module never parses the raw
# marker text itself (that remains the producer's responsibility).
def _is_producer_authorized_runtime_only_skip(item: dict[str, Any]) -> bool:
    """Recognize only a complete runtime_only skip produced by the VC producer."""
    return (
        item.get("runner") == "skipped"
        and item.get("scope_class") == "runtime_only"
        and item.get("classification") == "skipped"
        and item.get("decision") == "go"
        and item.get("category") == "preflight_scope_runtime_only"
        and item.get("verification_owner") == "impl-review-loop"
        and isinstance(item.get("deferred_reason"), str)
        and bool(item["deferred_reason"])
        and item.get("runtime_verification_required") is True
    )


# Issue #2467 P0-1 review fix (PR #2483 REQUEST_CHANGES): the baseline
# canonical runtime_only skip recognized by
# _is_producer_authorized_runtime_only_skip() above is delegation
# AUTHORIZATION only -- it permits the deferred command's real execution to
# be delegated to post-implementation test-runner. It must never be
# re-required on the CURRENT side; the current side is supposed to carry the
# actual execution evidence for that same (ac, command_hash), not another
# skip declaration. This recognizer instead requires the current item to
# carry real per-command execution facts (populated by
# adapt_test_verdict_to_current_vc_result() from a TEST_VERDICT_MACHINE/v2
# runtime_ac_results[] entry): status == "pass", exit_code == 0, and no
# fallback / human-review / stop-condition flags for that specific command.
def _is_runtime_only_current_execution_pass(item: dict[str, Any]) -> bool:
    """Recognize a current-head runtime_only item as an ACTUAL executed PASS."""
    return (
        item.get("status") == "pass"
        and item.get("exit_code") == 0
        and item.get("fallback_detected") is False
        and item.get("human_review_required") is False
        and item.get("stop_condition_triggered") is False
    )


def _load_path_list(raw: Any) -> tuple[list[str], list[str]]:
    if raw is None:
        return [], ["missing_path_input"]
    if not isinstance(raw, list):
        return [], ["invalid_path_input"]
    return [item for item in raw if isinstance(item, str)], []


def _extract_changed_paths(diff_summary: Any) -> tuple[list[str], bool, list[str]]:
    if diff_summary is None:
        return [], False, ["missing_diff_summary"]
    if not isinstance(diff_summary, dict):
        return [], False, ["invalid_diff_summary"]

    raw_paths = None
    for key in ("changed_paths", "changed", "paths", "files"):
        if key in diff_summary:
            raw_paths = diff_summary.get(key)
            break

    if isinstance(raw_paths, list):
        values: list[str] = []
        unrecognized_records: list[str] = []
        for item in raw_paths:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                matched = False
                for key in (
                    "path",
                    "file",
                    "filename",
                    "previous_path",
                    "previous_filename",
                    "old_path",
                ):
                    value = item.get(key)
                    if isinstance(value, str):
                        values.append(value)
                        matched = True
                if not matched and item:
                    unrecognized_records.append("unrecognized_changed_path_record")
        if unrecognized_records:
            return values, bool(values), unrecognized_records
        return values, bool(values), []

    return [], False, ["missing_changed_paths"]


def _normalize_path(path: str) -> str:
    return path.rstrip("/")


def _get_allowed_paths_matcher():
    global _ALLOWED_PATHS_MATCHER
    if _ALLOWED_PATHS_MATCHER is not None:
        return _ALLOWED_PATHS_MATCHER, None

    spec = importlib.util.spec_from_file_location(
        "allowed_paths_review_gate_for_adjudicator",
        _ALLOWED_PATHS_GATE_PATH,
    )
    if spec is None or spec.loader is None:
        return None, "allowed_paths_matcher_unavailable"
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover
        return None, f"allowed_paths_matcher_import_failed:{type(exc).__name__}"
    _ALLOWED_PATHS_MATCHER = module.AllowedPathsMatcher
    return _ALLOWED_PATHS_MATCHER, None


def _failure_key_root(failure_key: dict[str, str]) -> str | None:
    kind = failure_key["kind"]
    key = failure_key["key"].strip()
    if kind not in PATH_RELEVANCE_KINDS:
        return None
    if "::" in key:
        return key.split("::", 1)[0]
    return key


def _normalize_allowed_paths(allowed_paths: list[str]) -> tuple[list[str], str | None]:
    matcher, error = _get_allowed_paths_matcher()
    if matcher is None:
        return [], error

    normalized: list[str] = []
    for path in allowed_paths:
        normalized_path = matcher.normalize_allowed_pattern(path)
        if normalized_path is None:
            return [], f"invalid_allowed_path_pattern:{path}"
        normalized.append(normalized_path)
    return normalized, None


def _normalize_scope_paths(paths: list[str]) -> list[str]:
    return [_normalize_path(path) for path in paths if isinstance(path, str) and path.strip()]


def _all_changed_paths_allowed(changed_paths: list[str], allowed_paths: list[str]) -> bool:
    matcher, error = _get_allowed_paths_matcher()
    if matcher is None or error is not None:
        return False
    for path in changed_paths:
        normalized_path = matcher.normalize_path(path)
        if normalized_path is None or not any(
            matcher.matches_pattern(normalized_path, pattern) for pattern in allowed_paths
        ):
            return False
    return True


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _test_verdict_binding_error(
    test_verdict: Any,
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    expected_keys: set[tuple[str, str]],
    require_producer_receipt: bool = False,
) -> str | None:
    """Return a fail-closed reason unless runtime execution evidence is bound."""
    if not isinstance(test_verdict, dict):
        return "test_verdict_missing"
    if not isinstance(contract_snapshot, dict) or not isinstance(current_vc_result, dict):
        return "test_verdict_binding_context_invalid"
    if not isinstance(diff_summary, dict):
        return "test_verdict_diff_context_invalid"

    expected_issue = current_vc_result.get("issue")
    expected_pr = diff_summary.get("pr_number")
    expected_head = current_vc_result.get("head_sha")
    expected_reviewed_head = current_vc_result.get("reviewed_head_sha")
    expected_diff_head = diff_summary.get("head_sha")
    expected_contract_sha = contract_snapshot.get("body_sha256")
    required_bindings = {
        "issue_number": expected_issue,
        "pr_number": expected_pr,
        "head_sha": expected_head,
        "reviewed_head_sha": expected_reviewed_head,
        "diff_head_sha": expected_diff_head,
        "contract_body_sha256": expected_contract_sha,
    }
    if test_verdict.get("schema") != TEST_VERDICT_SCHEMA:
        return "test_verdict_schema_mismatch"
    for key, expected in required_bindings.items():
        if expected is None or test_verdict.get(key) != expected:
            return f"test_verdict_{key}_mismatch"
    if not _is_nonempty_string(test_verdict.get("run_id")):
        return "test_verdict_run_id_missing"
    run_url = test_verdict.get("run_url")
    if not isinstance(run_url, str) or not run_url.startswith("https://"):
        return "test_verdict_run_url_invalid"
    if test_verdict.get("result") != "PASS":
        return "test_verdict_result_not_pass"
    if test_verdict.get("verification_commands_fail") != 0:
        return "test_verdict_fail_count_nonzero"
    if test_verdict.get("verification_skipped_count") != 0:
        return "test_verdict_skip_count_nonzero"

    # A v2 verdict is only usable when it identifies the producer and the
    # GitHub Actions artifact that was read back.  The artifact payload is
    # bound to the digest recorded by that readback, so a copied or partial
    # summary cannot stand in for current-head execution evidence.
    if test_verdict.get("producer_kind") != "test-runner":
        return "test_verdict_producer_kind_mismatch"
    if not _is_nonempty_string(test_verdict.get("repository")):
        return "test_verdict_repository_missing"
    for key in ("workflow_run_id", "workflow_run_attempt", "check_run_id"):
        value = test_verdict.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return f"test_verdict_{key}_invalid"
    artifact = test_verdict.get("artifact")
    if not isinstance(artifact, dict):
        return "test_verdict_artifact_missing"
    if not _is_nonempty_string(artifact.get("name")):
        return "test_verdict_artifact_name_missing"
    artifact_digest = artifact.get("artifact_digest")
    if (
        not isinstance(artifact_digest, str)
        or not artifact_digest.startswith("sha256:")
        or not _is_hex_64(artifact_digest[7:])
    ):
        return "test_verdict_artifact_digest_invalid"
    artifact_url = artifact.get("url")
    if not isinstance(artifact_url, str) or not artifact_url.startswith("https://github.com/"):
        return "test_verdict_artifact_url_invalid"
    artifact_payload = test_verdict.get("artifact_payload")
    if not isinstance(artifact_payload, dict):
        return "test_verdict_artifact_payload_missing"
    artifact_payload_sha256 = test_verdict.get("artifact_payload_sha256")
    if (
        not isinstance(artifact_payload_sha256, str)
        or not artifact_payload_sha256.startswith("sha256:")
        or not _is_hex_64(artifact_payload_sha256[7:])
    ):
        return "test_verdict_artifact_payload_sha256_invalid"
    if _sha256(_canonical_json(artifact_payload)) != artifact_payload_sha256:
        return "test_verdict_artifact_digest_mismatch"
    for key, expected in required_bindings.items():
        if artifact_payload.get(key) != expected:
            return f"test_verdict_artifact_{key}_mismatch"
    if artifact_payload.get("command_hashes") != sorted(command_hash for _, command_hash in expected_keys):
        return "test_verdict_artifact_command_hashes_mismatch"

    raw_results = test_verdict.get("runtime_ac_results")
    if not isinstance(raw_results, list):
        return "test_verdict_runtime_ac_results_missing"
    observed_keys: set[tuple[str, str]] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            return "test_verdict_runtime_ac_result_invalid"
        ac = item.get("ac")
        command_hash = item.get("command_hash")
        if not isinstance(ac, str) or not isinstance(command_hash, str):
            return "test_verdict_runtime_ac_identity_missing"
        key = (ac, command_hash)
        if key in observed_keys:
            return f"test_verdict_runtime_ac_duplicate:{ac}"
        observed_keys.add(key)
        if (
            item.get("status") != "pass"
            or item.get("exit_code") != 0
            or item.get("fallback_detected") is not False
            or item.get("human_review_required") is not False
            or item.get("stop_condition_triggered") is not False
        ):
            return f"test_verdict_runtime_ac_not_executed_pass:{ac}"
    if observed_keys != expected_keys:
        return "test_verdict_runtime_ac_coverage_mismatch"

    if require_producer_receipt:
        receipt = test_verdict.get("producer_receipt")
        if not isinstance(receipt, dict):
            return "test_verdict_producer_receipt_missing"
        validator, validator_error = _get_producer_receipt_validator()
        if validator is None:
            return f"test_verdict_producer_receipt_validator_unavailable:{validator_error}"
        if list(validator.iter_errors(receipt)):
            return "test_verdict_producer_receipt_schema_invalid"
        receipt_sha256 = test_verdict.get("receipt_sha256")
        if receipt_sha256 != _sha256(_canonical_json(receipt)):
            return "test_verdict_receipt_sha256_mismatch"
        if receipt.get("pass_eligible") is not True:
            return "test_verdict_receipt_not_pass_eligible"
        receipt_subject = receipt.get("subject")
        if not isinstance(receipt_subject, dict) or receipt_subject.get("pr_head_sha") != test_verdict.get("head_sha"):
            return "test_verdict_receipt_subject_head_sha_mismatch"
        if receipt_subject.get("target_pr_number") != test_verdict.get("pr_number"):
            return "test_verdict_receipt_subject_pr_number_mismatch"
        receipt_contract = receipt.get("contract")
        if not isinstance(receipt_contract, dict) or receipt_contract.get("linked_issue_number") != test_verdict.get(
            "issue_number"
        ):
            return "test_verdict_receipt_contract_issue_number_mismatch"
        if receipt_contract.get("issue_body_sha256") != test_verdict.get("contract_body_sha256"):
            return "test_verdict_receipt_contract_body_sha256_mismatch"
        receipt_artifact = receipt.get("execution_artifact")
        if not isinstance(receipt_artifact, dict) or receipt_artifact.get("artifact_archive_digest") != artifact_digest:
            return "test_verdict_receipt_artifact_digest_mismatch"

    return None


# Issue #2467 AC2/AC3: current-head independent binding for a
# runtime_only producer skip. Unlike _test_verdict_binding_error() above,
# this does NOT require a GitHub Actions TEST_VERDICT/artifact readback --
# per Issue #2467 In Scope, artifact / receipt / materialized TEST_VERDICT is
# optional diagnostic provenance for runtime_only, not a mandatory input.
# The binding instead uses the orchestrator's own current-head evidence
# (contract_snapshot / current_vc_result / diff_summary), which is exactly
# what the orchestrator independently binds to the current head before
# calling adjudicate_vc_result() (Issue #2467 In Scope: "current routing
# authority は ... orchestrator が独立に current-head へ binding して生成する
# VC_ADJUDICATION_RESULT_V1"). A baseline skip declaration alone is
# insufficient -- every one of the checks below must hold, fail-closed.
def _runtime_only_current_head_binding_error(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    changed_paths: list[str],
    changed_paths_present: bool,
    allowed_paths: list[str],
    expected_issue_number: Any = None,
    expected_pr_number: Any = None,
    allow_delegated_nonpass: bool = False,
    delegated_keys: frozenset[tuple[str, str]] = frozenset(),
) -> str | None:
    """runtime_only current-head independent binding (Issue #2467).

    Issue #2916: when a pr_review_only non-pass is delegated in the same report,
    the report-level aggregate is legitimately fail / partial. The runtime_only
    ACs' own executed-PASS requirement is enforced per item by the caller and
    every non-delegated row's fallback is still checked on its own."""
    return _current_head_binding_error(
        reason_prefix="runtime_only",
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        changed_paths=changed_paths,
        changed_paths_present=changed_paths_present,
        allowed_paths=allowed_paths,
        expected_issue_number=expected_issue_number,
        expected_pr_number=expected_pr_number,
        allow_delegated_nonpass=allow_delegated_nonpass,
        delegated_keys=delegated_keys,
    )


def _non_delegated_rows_are_fallback_free(current_vc_result: Any, delegated_keys: frozenset[tuple[str, str]]) -> bool:
    """Issue #2916: with a delegated non-pass row the report-level
    ``fallback_detected`` aggregate is not used (the delegated row itself may
    carry it), so EVERY OTHER row is checked on its own: ``fallback_detected``
    must be exactly False. A fallback on an ordinary / runtime_only / non-delegated
    row is never excused by a different row's delegation (no PASS conversion)."""
    rows = current_vc_result.get("results") if isinstance(current_vc_result, dict) else None
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        if (row.get("ac"), row.get("command_hash")) in delegated_keys:
            continue
        if row.get("fallback_detected") is not False:
            return False
    return True


# Issue #2912: the pr_review_only INDEPENDENT route binds current-head evidence
# exactly like runtime_only does (the same helper, so the two cannot drift),
# but every reason code carries the `pr_review_only_` prefix and the report's
# own PR number is bound as well. It never requires a GitHub workflow / check /
# artifact identifier: it is selected only by
# _pr_review_only_uses_independent_route() below.
def _pr_review_only_current_head_binding_error(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    changed_paths: list[str],
    changed_paths_present: bool,
    allowed_paths: list[str],
    expected_issue_number: Any = None,
    expected_pr_number: Any = None,
    allow_delegated_nonpass: bool = False,
    delegated_keys: frozenset[tuple[str, str]] = frozenset(),
) -> str | None:
    return _current_head_binding_error(
        reason_prefix="pr_review_only",
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        changed_paths=changed_paths,
        changed_paths_present=changed_paths_present,
        allowed_paths=allowed_paths,
        expected_issue_number=expected_issue_number,
        expected_pr_number=expected_pr_number,
        bind_report_pr_number=True,
        allow_delegated_nonpass=allow_delegated_nonpass,
        delegated_keys=delegated_keys,
    )


# Issue #2912: GitHub-specific trust markers. The mere PRESENCE of any of these
# keys in the test_verdict (even with a null / empty / placeholder value) means
# the report claims GitHub-derived provenance, so it must be validated by the
# legacy _test_verdict_binding_error() and can never be independent evidence.
# producer_kind / repository / run_id / run_url are descriptive only: allowed,
# never required, never a trust root.
_PR_REVIEW_ONLY_TRUST_MARKER_KEYS = (
    "workflow_run_id",
    "workflow_run_attempt",
    "check_run_id",
    "artifact",
    "artifact_payload",
    "artifact_payload_sha256",
    "producer_receipt",
    "receipt_sha256",
)


def _pr_review_only_uses_independent_route(test_verdict: Any, *, require_producer_receipt: bool) -> bool:
    """Decide ONCE, before any per-item validation, which route a pr_review_only
    adjudication takes (Issue #2912). Legacy (False) when any of:

    1. --require-producer-receipt was requested,
    2. test_verdict is not a dict (missing / None),
    3. any GitHub trust marker KEY is present (regardless of its value), or
    4. the dict is not a TEST_VERDICT_MACHINE/v2 report (a ``{"TEST_VERDICT": ...}``
       wrapper, a different schema, or ``{}``): these must keep failing closed
       through the legacy ``test_verdict_schema_mismatch`` validation.
    """
    if require_producer_receipt:
        return False
    if not isinstance(test_verdict, dict):
        return False
    if test_verdict.get("schema") != TEST_VERDICT_SCHEMA:
        return False
    return not any(key in test_verdict for key in _PR_REVIEW_ONLY_TRUST_MARKER_KEYS)


def _is_pr_review_only_current_execution_pass(item: dict[str, Any]) -> bool:
    """An adapter-derived current item that is an ACTUAL executed PASS (the
    same per-command facts runtime_only requires)."""
    return _is_runtime_only_current_execution_pass(item)


def _is_pr_review_only_skip_echo(item: dict[str, Any]) -> bool:
    """A current item that merely echoes a skip declaration (no execution)."""
    return (
        _is_producer_authorized_pr_review_only_skip(item)
        or item.get("runner") == "skipped"
        or item.get("classification") == "skipped"
    )


_PR_REVIEW_ONLY_RAW_EXECUTION_FLAGS = ("fallback_detected", "human_review_required", "stop_condition_triggered")


# Issue #2916: reviewer delegation of an EXECUTED non-pass pr_review_only item.
#
# A non-pass execution fact is never rewritten into PASS and never covered by
# skip metadata. When (and only when) the caller opts in through
# ``--delegate-pr-review-only-nonpass``, the independent route records the fact
# LOSSLESSLY in the existing per_ac ``failure_keys`` field (``{kind, key}``
# rows; no new schema / key set) and marks the entry
# ``status: indeterminate`` / ``blocking: true`` with the reason code below.
# ``evaluate_step4_vc_gate()`` then permits a reviewer DISPATCH for exactly that
# shape; the AC stays unresolved (``blocking: true``) so dispatch permission is
# neither AC achievement nor terminal approval. Terminal approval stays the
# exclusive decision of ``step5_terminal_gate()`` (an ``approved`` reviewer route
# plus the dispatch / binding / gate checks).
REASON_PR_REVIEW_ONLY_NONPASS_DELEGATED = "pr_review_only_nonpass_delegated_to_reviewer"
_NONPASS_FACT_KIND = "pr_review_only_current_execution_fact"
_NONPASS_FACT_NAMES = (
    "exit_code",
    "status",
    "fallback_detected",
    "human_review_required",
    "stop_condition_triggered",
)


def _nonpass_facts_of(item: dict[str, Any]) -> dict[str, Any]:
    """The per-command execution facts of an adapter-derived current item."""
    return {name: item.get(name) for name in _NONPASS_FACT_NAMES}


def _is_delegable_nonpass_facts(facts: dict[str, Any]) -> bool:
    """True only for COHERENT executed non-pass facts that a reviewer may judge.

    Delegable (mirrors the test-runner classification table): ``fail`` /
    ``skip`` with a non-zero exit, or ``fail`` with exit 0 that carries
    ``fallback_detected: true`` (a fallback success is classified FAIL, never
    PASS). Everything else stays fail-closed: ``fail`` / exit 0 WITHOUT a
    fallback and ``skip`` / exit 0 (a failure dressed up as success), ``pass``
    in any form (including ``pass`` + ``fallback_detected: true``, which the
    producer never emits), unknown status values, non-bool flags, and any
    ``human_review_required`` / ``stop_condition_triggered`` (explicit human /
    stop signals are never delegated to the reviewer)."""
    exit_code = facts.get("exit_code")
    status = facts.get("status")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return False
    fallback = facts.get("fallback_detected")
    if not isinstance(fallback, bool):
        return False
    if facts.get("human_review_required") is not False or facts.get("stop_condition_triggered") is not False:
        return False
    if status == "fail":
        return exit_code != 0 or fallback is True
    if status == "skip":
        return exit_code != 0
    return False


def _encode_nonpass_facts(facts: dict[str, Any]) -> list[dict[str, str]]:
    """Lossless ``failure_keys`` rows: ``name=<json value>`` per fact."""
    return [
        {"kind": _NONPASS_FACT_KIND, "key": f"{name}={json.dumps(facts.get(name))}"}
        for name in _NONPASS_FACT_NAMES
    ]


def _decode_nonpass_facts(failure_keys: Any) -> dict[str, Any] | None:
    """Strict inverse of ``_encode_nonpass_facts``; None when malformed."""
    if not isinstance(failure_keys, list) or len(failure_keys) != len(_NONPASS_FACT_NAMES):
        return None
    facts: dict[str, Any] = {}
    for row in failure_keys:
        if not isinstance(row, dict) or row.get("kind") != _NONPASS_FACT_KIND:
            return None
        key = row.get("key")
        if not isinstance(key, str) or "=" not in key:
            return None
        name, _, raw = key.partition("=")
        if name not in _NONPASS_FACT_NAMES or name in facts:
            return None
        try:
            facts[name] = json.loads(raw)
        except ValueError:
            return None
    if set(facts) != set(_NONPASS_FACT_NAMES):
        return None
    return facts


def _is_valid_delegated_nonpass_entry(entry: Any) -> bool:
    """Shape check of a persisted per_ac entry that delegates a non-pass
    execution to the reviewer. A forged / hand-edited entry (facts that are
    not an executed non-pass, an entry claiming ``pass`` / non-blocking, a
    missing identity) never opens the Step 4 gate."""
    if not isinstance(entry, dict):
        return False
    if entry.get("reason_code") != REASON_PR_REVIEW_ONLY_NONPASS_DELEGATED:
        return False
    if entry.get("status") != "indeterminate" or entry.get("blocking") is not True:
        return False
    if not _is_nonempty_string(entry.get("ac")) or not _is_nonempty_string(entry.get("command_hash")):
        return False
    facts = _decode_nonpass_facts(entry.get("failure_keys"))
    return facts is not None and _is_delegable_nonpass_facts(facts)


def _pr_review_only_raw_report_error(test_verdict: Any) -> str | None:
    """Fail-closed validation of the RAW TEST_VERDICT_MACHINE/v2 report for the
    independent pr_review_only route (Issue #2912 fix_delta, PR #2924 P2).

    adapt_test_verdict_to_current_vc_result() coerces the execution flags with
    bool() and back-fills reviewed_head_sha from head_sha, so a missing / null /
    non-bool flag or a missing reviewed_head_sha is indistinguishable from an
    explicit false / matching head once adapted. step-2 requires head_sha /
    reviewed_head_sha / diff_head_sha bound to one head and per-command
    execution flags, so they are verified here on the report as received."""
    if not isinstance(test_verdict, dict):
        return "pr_review_only_report_not_object"
    head_sha = test_verdict.get("head_sha")
    if (
        not _is_nonempty_string(head_sha)
        or test_verdict.get("reviewed_head_sha") != head_sha
        or test_verdict.get("diff_head_sha") != head_sha
    ):
        return "pr_review_only_head_binding_mismatch"
    if "human_review_required" in test_verdict and not isinstance(test_verdict["human_review_required"], bool):
        return "pr_review_only_report_flag_not_boolean::human_review_required"
    rows = test_verdict.get("runtime_ac_results")
    if not isinstance(rows, list):
        return None  # missing_runtime_ac_results is already reported by the adapter
    for row in rows:
        if not isinstance(row, dict):
            continue
        for flag in _PR_REVIEW_ONLY_RAW_EXECUTION_FLAGS:
            if not isinstance(row.get(flag), bool):
                return f"pr_review_only_report_flag_not_boolean:{row.get('ac')}:{flag}"
    return None


def _current_head_binding_error(
    *,
    reason_prefix: str,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    changed_paths: list[str],
    changed_paths_present: bool,
    allowed_paths: list[str],
    expected_issue_number: Any = None,
    expected_pr_number: Any = None,
    bind_report_pr_number: bool = False,
    allow_delegated_nonpass: bool = False,
    delegated_keys: frozenset[tuple[str, str]] = frozenset(),
) -> str | None:
    """Return a fail-closed reason unless an independent current-head
    binding (Issue / PR / current head / reviewed head / diff
    head / Issue body digest / source integrity) is fully satisfied.

    Issue #2467 P1 review fix (PR #2483 REQUEST_CHANGES): the Issue / PR
    checks below require exact equality against ``expected_issue_number`` /
    ``expected_pr_number`` -- values the caller (root/orchestrator)
    independently retrieved from the live Issue and current PR -- not merely
    positive-integer presence in the evidence payload itself."""
    if not isinstance(contract_snapshot, dict) or not isinstance(current_vc_result, dict):
        return f"{reason_prefix}_binding_context_invalid"
    if not isinstance(diff_summary, dict):
        return f"{reason_prefix}_diff_context_invalid"

    if contract_snapshot.get("status") != "go":
        return f"{reason_prefix}_contract_not_go"
    contract_sha = contract_snapshot.get("body_sha256")
    if not _is_nonempty_string(contract_sha):
        return f"{reason_prefix}_contract_body_sha256_missing"

    if not _is_nonempty_string(current_vc_result.get("generated_at")):
        return f"{reason_prefix}_generated_at_missing"
    if allow_delegated_nonpass:
        # Issue #2916: at least one executed non-pass item is delegated to the
        # reviewer, so the report-level result must itself be non-pass. A
        # report-level PASS next to a failing row is a failure dressed up as
        # success and is refused; the delegated row's own recorded fallback fact is not an error here
        # (every other row is checked on its own below).
        if current_vc_result.get("status") not in {"fail", "partial"}:
            return f"{reason_prefix}_nonpass_report_result_inconsistent"
        if current_vc_result.get("errors") != []:
            return f"{reason_prefix}_current_vc_result_errors_present"
        # The report-level fallback aggregate is not used here (the delegated
        # row may carry the recorded fallback fact); every OTHER row is checked
        # on its own so no non-delegated row's fallback is excused.
        if not _non_delegated_rows_are_fallback_free(current_vc_result, delegated_keys):
            return f"{reason_prefix}_fallback_detected"
    else:
        if current_vc_result.get("status") != "pass":
            return f"{reason_prefix}_current_vc_result_not_pass"
        if current_vc_result.get("errors") != []:
            return f"{reason_prefix}_current_vc_result_errors_present"
        if current_vc_result.get("fallback_detected") is not False:
            return f"{reason_prefix}_fallback_detected"
    if current_vc_result.get("human_review_required") is not False:
        return f"{reason_prefix}_human_review_required"
    if current_vc_result.get("stop_condition_triggered") is not False:
        return f"{reason_prefix}_stop_condition_triggered"

    current_head = current_vc_result.get("head_sha")
    reviewed_head = current_vc_result.get("reviewed_head_sha")
    diff_head = diff_summary.get("head_sha")
    if (
        not _is_nonempty_string(current_head)
        or current_head != reviewed_head
        or current_head != diff_head
    ):
        return f"{reason_prefix}_head_binding_mismatch"

    source = current_vc_result.get("source")
    if not isinstance(source, dict) or source.get("body_sha256") != contract_sha:
        return f"{reason_prefix}_source_body_sha256_mismatch"

    issue_number = current_vc_result.get("issue")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
        return f"{reason_prefix}_issue_number_missing"
    if (
        isinstance(expected_issue_number, bool)
        or not isinstance(expected_issue_number, int)
        or expected_issue_number <= 0
    ):
        return f"{reason_prefix}_expected_issue_number_missing"
    if issue_number != expected_issue_number:
        return f"{reason_prefix}_issue_number_mismatch"

    pr_number = diff_summary.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return f"{reason_prefix}_pr_number_missing"
    if (
        isinstance(expected_pr_number, bool)
        or not isinstance(expected_pr_number, int)
        or expected_pr_number <= 0
    ):
        return f"{reason_prefix}_expected_pr_number_missing"
    if pr_number != expected_pr_number:
        return f"{reason_prefix}_pr_number_mismatch"
    if bind_report_pr_number and current_vc_result.get("pr_number") != expected_pr_number:
        # Issue #2912: the independent pr_review_only route also binds the PR
        # number the report itself claims, not only the diff summary's.
        return f"{reason_prefix}_pr_number_mismatch"

    if changed_paths_present is not True:
        return f"{reason_prefix}_changed_paths_missing"
    if not _all_changed_paths_allowed(changed_paths, allowed_paths):
        return f"{reason_prefix}_changed_paths_not_certified"

    return None


def _current_pass_envelope_is_certified(
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    changed_paths: list[str],
    changed_paths_present: bool,
    allowed_paths: list[str],
    allow_delegated_nonpass_aggregate: bool = False,
    delegated_keys: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    if not isinstance(contract_snapshot, dict) or not isinstance(current_vc_result, dict):
        return False
    if not isinstance(diff_summary, dict):
        return False
    contract_sha = contract_snapshot.get("body_sha256")
    source = current_vc_result.get("source")
    if not isinstance(source, dict):
        return False
    current_head = current_vc_result.get("head_sha")
    reviewed_head = current_vc_result.get("reviewed_head_sha")
    diff_head = diff_summary.get("head_sha")
    # Issue #2916: when an executed non-pass pr_review_only item is delegated to
    # the reviewer the report-level result is non-pass by construction; every
    # OTHER binding check stays. This only certifies the ordinary rows' own
    # envelope (their own exit_code / status are still checked per row).
    if allow_delegated_nonpass_aggregate:
        aggregate_ok = current_vc_result.get("status") in {"fail", "partial"} and _non_delegated_rows_are_fallback_free(
            current_vc_result, delegated_keys
        )
    else:
        aggregate_ok = (
            current_vc_result.get("status") == "pass"
            and current_vc_result.get("fallback_detected") is False
        )
    return (
        contract_snapshot.get("status") == "go"
        and _is_nonempty_string(contract_sha)
        and _is_nonempty_string(current_vc_result.get("generated_at"))
        and aggregate_ok
        and current_vc_result.get("errors") == []
        and current_vc_result.get("human_review_required") is False
        and current_vc_result.get("stop_condition_triggered") is False
        and _is_nonempty_string(current_head)
        and current_head == reviewed_head == diff_head
        and source.get("body_sha256") == contract_sha
        and changed_paths_present is True
        and _all_changed_paths_allowed(changed_paths, allowed_paths)
    )


def _is_related_to_scope(
    failure_keys: list[dict[str, str]],
    changed_paths: list[str],
    allowed_paths: list[str],
) -> tuple[bool, bool]:
    matcher, error = _get_allowed_paths_matcher()
    if matcher is None or error is not None:
        return False, False

    scope = [*_normalize_scope_paths(changed_paths), *allowed_paths]
    for failure_key in failure_keys:
        root = _failure_key_root(failure_key)
        if root is None:
            return False, False
        normalized_root = matcher.normalize_path(root)
        if normalized_root is None:
            return False, False
        for path in scope:
            if matcher.matches_pattern(normalized_root, path):
                return True, True
    return False, True


def _extract_source_integrity(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: Any,
    allowed_paths: list[str] | None,
    normalized_allowed: list[str],
    baseline_schema: str | None,
    current_items_count: int,
    baseline_items_count: int,
    changed_paths_present: bool,
) -> dict[str, Any]:
    contract_body_sha256 = None
    if isinstance(contract_snapshot, dict):
        contract_body_sha256 = contract_snapshot.get("body_sha256")

    base_sha = None
    head_sha = None
    reviewed_head_sha = None
    current_vc_result_head_sha = None
    diff_summary_head_sha = None
    if isinstance(diff_summary, dict):
        base_sha = diff_summary.get("base_sha")
        head_sha = diff_summary.get("head_sha")
        diff_summary_head_sha = diff_summary.get("head_sha")
    if isinstance(current_vc_result, dict):
        current_vc_result_head_sha = current_vc_result.get("head_sha")
        reviewed_head_sha = current_vc_result.get("reviewed_head_sha")

    if isinstance(current_vc_result_head_sha, str) and current_vc_result_head_sha:
        head_sha = current_vc_result_head_sha
    if isinstance(reviewed_head_sha, str) and reviewed_head_sha:
        head_sha = head_sha or reviewed_head_sha

    evidence_fresh = True
    if (
        isinstance(diff_summary_head_sha, str)
        and isinstance(current_vc_result_head_sha, str)
        and diff_summary_head_sha
        and current_vc_result_head_sha
        and diff_summary_head_sha != current_vc_result_head_sha
    ):
        evidence_fresh = False

    return {
        "contract_snapshot_present": contract_snapshot is not None,
        "current_vc_result_present": current_vc_result is not None,
        "diff_summary_present": diff_summary is not None,
        "allowed_paths_present": allowed_paths is not None,
        "baseline_schema": baseline_schema,
        "current_items_count": current_items_count,
        "baseline_items_count": baseline_items_count,
        "changed_paths_present": changed_paths_present,
        "allowed_paths_normalized_sha256": _sha256(_canonical_json(normalized_allowed))
        if normalized_allowed
        else None,
        "contract_body_sha256": contract_body_sha256,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "current_vc_result_head_sha": current_vc_result_head_sha,
        "diff_summary_head_sha": diff_summary_head_sha,
        "evidence_complete": (
            contract_snapshot is not None
            and current_vc_result is not None
            and diff_summary is not None
            and allowed_paths is not None
        ),
        "evidence_fresh": evidence_fresh,
    }


def _build_evidence_refs(
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: dict[str, Any] | None,
    normalized_allowed: list[str] | None,
    test_verdict: Any | None,
) -> list[dict[str, str]]:
    refs = [
        {
            "kind": "contract_snapshot",
            "ref": "inline:contract_snapshot",
            "digest": _sha256(_canonical_json(contract_snapshot)),
            "validation_verdict": "pass",
        },
        {
            "kind": "current_vc_result",
            "ref": "inline:current_vc_result",
            "digest": _sha256(_canonical_json(current_vc_result)),
            "validation_verdict": "pass",
        },
    ]
    if diff_summary is not None:
        refs.append(
            {
                "kind": "diff_summary",
                "ref": "inline:diff_summary",
                "digest": _sha256(_canonical_json(diff_summary)),
                "validation_verdict": "pass",
            }
        )
    if normalized_allowed is not None:
        refs.append(
            {
                "kind": "allowed_paths",
                "ref": "inline:allowed_paths",
                "digest": _sha256(_canonical_json(normalized_allowed)),
                "validation_verdict": "pass",
            }
        )
    if test_verdict is not None:
        refs.append(
            {
                "kind": "test_verdict",
                "ref": "inline:test_verdict",
                "digest": _sha256(_canonical_json(test_verdict)),
                "validation_verdict": "pass",
            }
        )
    return refs


def _result(
    *,
    overall_status: str,
    per_ac: list[dict[str, Any]],
    rerun_required: bool,
    source_integrity: dict[str, Any],
    evidence_refs: list[dict[str, str]],
    artifact_ref: str | None = None,
    artifact_digest: str | None = None,
    errors: list[str] | None = None,
    stdout_truncated: bool = False,
    omitted_fields: list[str] | None = None,
) -> dict[str, Any]:
    result_errors = errors or []
    if overall_status == "pass" and not per_ac:
        overall_status = "indeterminate"
        rerun_required = True
        result_errors = [*result_errors, "pass_requires_per_ac_coverage"]
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "overall_status": overall_status,
        "blocking": (
            any(item["blocking"] for item in per_ac)
            if per_ac
            else overall_status not in {"pass", "pre_existing_fail", "out_of_scope_fail"}
        ),
        "rerun_required": rerun_required,
        "per_ac": per_ac,
        "evidence_refs": evidence_refs,
        "source_integrity": source_integrity,
        "errors": result_errors,
        "artifact_ref": artifact_ref,
        "artifact_digest": artifact_digest,
        "stdout_truncated": stdout_truncated,
        "omitted_fields": omitted_fields or [],
    }


def _classify_item(
    item: dict[str, Any],
    baseline_signatures: set[tuple[str, tuple[tuple[str, str], ...]]],
    baseline_failure_index: set[tuple[str, str]],
    changed_paths: list[str],
    allowed_paths: list[str],
    source_integrity: dict[str, Any],
) -> tuple[str, bool, bool, str, str]:
    command_hash = item["command_hash"]
    failure_keys = item["failure_keys"]

    if item.get("exit_code") == 5 or item.get("category") == "vc_no_tests_collected":
        return "indeterminate", True, True, "pytest_exit_5", "Pytest exit code 5 is not treated as regression"

    if item.get("category") in ENVIRONMENT_BLOCKED_CATEGORIES:
        return "environment_blocked", True, True, "environment_signal", "Environment/tooling blocker detected"

    if not source_integrity["evidence_fresh"]:
        return "indeterminate", True, True, "stale_evidence", "Source evidence is stale for current head"

    baseline_key = (
        command_hash,
        tuple((entry["kind"], entry["key"]) for entry in failure_keys),
    )
    if baseline_key in baseline_signatures:
        if item["failure_keys_present"] and source_integrity["evidence_complete"] and not changed_paths:
            return (
                "pre_existing_fail",
                False,
                False,
                "same_baseline_no_diff",
                "Exact baseline signature with no diff and complete evidence",
            )
        return (
            "indeterminate",
            True,
            True,
            "baseline_match_inconclusive",
            "Baseline match exists but evidence is incomplete or diff is present",
        )

    if not item["failure_keys_present"]:
        return "indeterminate", True, True, "missing_failure_keys", "Failure key evidence is missing"

    if not changed_paths or not allowed_paths:
        return (
            "indeterminate",
            True,
            True,
            "insufficient_scope_evidence",
            "Diff scope evidence is incomplete for adjudication",
        )

    related, relevance_deterministic = _is_related_to_scope(
        failure_keys,
        changed_paths,
        allowed_paths,
    )
    if not relevance_deterministic:
        return (
            "indeterminate",
            True,
            True,
            "unsupported_failure_key_kind",
            "Failure key kind cannot prove scope irrelevance",
        )
    if related:
        return (
            "regression_fail",
            True,
            False,
            "related_to_changed_scope",
            "Failure is related to changed and/or allowed scope",
        )

    current_failure_index = {(entry["kind"], entry["key"]) for entry in failure_keys}
    if not current_failure_index.issubset(baseline_failure_index):
        return (
            "indeterminate",
            True,
            True,
            "new_failure_without_scope_proof",
            "New failure keys cannot be downgraded to out_of_scope without baseline match",
        )

    return (
        "out_of_scope_fail",
        False,
        False,
        "unrelated_to_scope_with_baseline_match",
        "Failure is unrelated to changed scope and was already present in baseline",
    )


def adjudicate_vc_result(
    *,
    contract_snapshot: Any,
    current_vc_result: Any,
    diff_summary: dict[str, Any] | None,
    allowed_paths: list[str] | None,
    test_verdict: Any | None = None,
    require_producer_receipt: bool = False,
    expected_issue_number: Any = None,
    expected_pr_number: Any = None,
    delegate_pr_review_only_nonpass: bool = False,
) -> dict[str, Any]:
    # Issue #1648 fix_delta AC9 (P1-3): a caller that forgets to pass
    # --require-producer-receipt must not silently accept a materialized
    # TEST_VERDICT_MACHINE/v2 bundle (which always embeds a producer_receipt)
    # as if it were self-attested. If the test_verdict itself carries a
    # producer_receipt field, receipt verification is forced on regardless
    # of the caller-supplied flag. Legacy self-attested TEST_VERDICT input
    # (no producer_receipt field at all) is unaffected -- non-regression.
    effective_require_producer_receipt = require_producer_receipt or (
        isinstance(test_verdict, dict) and "producer_receipt" in test_verdict
    )
    baseline_items, baseline_errors, baseline_schema = _normalize_list_payload(contract_snapshot)
    current_items, current_errors, _ = _normalize_list_payload(current_vc_result)
    changed_paths, changed_paths_present, diff_errors = _extract_changed_paths(diff_summary)
    allowed_path_values, allowed_paths_errors = _load_path_list(allowed_paths)
    normalized_allowed, normalize_allowed_error = _normalize_allowed_paths(allowed_path_values)
    if normalize_allowed_error is not None:
        allowed_paths_errors.append(normalize_allowed_error)

    source_integrity = _extract_source_integrity(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        normalized_allowed=normalized_allowed,
        baseline_schema=baseline_schema,
        current_items_count=len(current_items),
        baseline_items_count=len(baseline_items),
        changed_paths_present=changed_paths_present,
    )
    extraction_errors = list(baseline_errors + current_errors + diff_errors + allowed_paths_errors)
    evidence_refs = _build_evidence_refs(
        contract_snapshot,
        current_vc_result,
        diff_summary,
        normalized_allowed,
        test_verdict,
    )

    if extraction_errors:
        return _result(
            overall_status="indeterminate",
            rerun_required=True,
            per_ac=[],
            source_integrity=source_integrity,
            evidence_refs=evidence_refs,
            errors=extraction_errors,
        )

    baseline_signatures: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    baseline_failure_index: set[tuple[str, str]] = set()
    baseline_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    excluded_pr_review_only_keys: set[tuple[str, str]] = set()
    excluded_runtime_only_keys: set[tuple[str, str]] = set()
    seen_baseline_keys: set[tuple[str, str]] = set()
    for idx, item in enumerate(baseline_items):
        norm, errs = _normalize_item(item)
        if norm is None:
            return _result(
                overall_status="indeterminate",
                per_ac=[],
                rerun_required=True,
                source_integrity=source_integrity,
                evidence_refs=evidence_refs,
                errors=[f"baseline[{idx}]:{err}" for err in errs],
            )
        mapping_key = (norm["ac"], norm["command_hash"])
        if mapping_key in seen_baseline_keys:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[f"duplicate_baseline_ac_command_hash:{norm['ac']}"],
            )
        seen_baseline_keys.add(mapping_key)
        if _is_producer_authorized_pr_review_only_skip(norm):
            excluded_pr_review_only_keys.add(mapping_key)
            continue
        if _is_producer_authorized_runtime_only_skip(norm):
            excluded_runtime_only_keys.add(mapping_key)
            continue
        if norm["classification"] not in {"expected_fail", "expected_pass"}:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[f"unsupported_baseline_classification:{norm['ac']}"],
            )
        baseline_by_key[mapping_key] = norm
        baseline_signatures.add(
            (
                norm["command_hash"],
                tuple((entry["kind"], entry["key"]) for entry in norm["failure_keys"]),
            )
        )
        baseline_failure_index.update((entry["kind"], entry["key"]) for entry in norm["failure_keys"])

    # Issue #2912: decide the pr_review_only route (independent current-head
    # evidence vs legacy GitHub-artifact provenance) ONCE, before any per-item
    # validation, so the per-item check, the binding check and the per_ac
    # assembly below can never disagree about which route applies.
    independent_pr_review_only = bool(excluded_pr_review_only_keys) and _pr_review_only_uses_independent_route(
        test_verdict, require_producer_receipt=effective_require_producer_receipt
    )

    # Issue #2467 P0-2 review fix: `current_order` records every current item
    # in its ORIGINAL (Issue declaration) order together with its kind
    # ("normal" / "pr_review_only" / "runtime_only") so per_ac can later be
    # assembled in that same literal order instead of re-sorting resolved
    # skip keys (which could reorder a multi-AC binding tuple relative to
    # the live Issue's Verification Commands, breaking evaluate_step4_vc_gate()'s
    # ordered command-hash comparison).
    current_order: list[dict[str, Any]] = []
    current_keys: set[tuple[str, str]] = set()
    seen_current_keys: set[tuple[str, str]] = set()
    excluded_current_count = 0
    excluded_current_keys: set[tuple[str, str]] = set()
    # Issue #2916: (ac, command_hash) keys of executed non-pass pr_review_only
    # items that are delegated to the reviewer (opt-in, independent route only).
    delegated_nonpass_keys: set[tuple[str, str]] = set()
    for idx, item in enumerate(current_items):
        norm, errs = _normalize_item(item)
        if norm is None:
            return _result(
                overall_status="indeterminate",
                per_ac=[],
                rerun_required=True,
                source_integrity=source_integrity,
                evidence_refs=evidence_refs,
                errors=[f"current[{idx}]:{err}" for err in errs],
            )

        mapping_key = (norm["ac"], norm["command_hash"])
        if mapping_key in seen_current_keys:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[f"duplicate_current_ac_command_hash:{norm['ac']}"],
            )
        seen_current_keys.add(mapping_key)
        if mapping_key in excluded_pr_review_only_keys and independent_pr_review_only:
            # Issue #2912 independent route: the baseline envelope is scope
            # authorization only. The CURRENT side must carry an ACTUAL
            # executed PASS for this (ac, command_hash); a skip-envelope echo
            # is not execution evidence and a non-PASS execution is never
            # rewritten into PASS or covered by skip metadata.
            if _is_pr_review_only_skip_echo(norm):
                return _result(
                    overall_status="indeterminate", per_ac=[], rerun_required=True,
                    source_integrity=source_integrity, evidence_refs=evidence_refs,
                    errors=[f"pr_review_only_independent_requires_executed_item:{norm['ac']}"],
                )
            if not _is_pr_review_only_current_execution_pass(norm):
                # Issue #2916: an EXECUTED, coherent non-pass fact (FAIL / SKIP /
                # fallback) may be delegated to the reviewer when the caller opted
                # in. The fact is recorded verbatim below (never PASS, never
                # covered by skip metadata); anything else keeps failing closed.
                if not (
                    delegate_pr_review_only_nonpass
                    and _is_delegable_nonpass_facts(_nonpass_facts_of(norm))
                ):
                    return _result(
                        overall_status="indeterminate", per_ac=[], rerun_required=True,
                        source_integrity=source_integrity, evidence_refs=evidence_refs,
                        errors=[f"pr_review_only_current_execution_not_pass:{norm['ac']}"],
                    )
                delegated_nonpass_keys.add(mapping_key)
            excluded_current_count += 1
            excluded_current_keys.add(mapping_key)
            current_order.append({"kind": "pr_review_only", "norm": norm})
            continue
        if mapping_key in excluded_pr_review_only_keys:
            if not _is_producer_authorized_pr_review_only_skip(norm):
                return _result(
                    overall_status="indeterminate", per_ac=[], rerun_required=True,
                    source_integrity=source_integrity, evidence_refs=evidence_refs,
                    errors=[f"pr_review_only_current_authorization_mismatch:{norm['ac']}"],
                )
            excluded_current_count += 1
            excluded_current_keys.add(mapping_key)
            current_order.append({"kind": "pr_review_only", "norm": norm})
            continue
        if mapping_key in excluded_runtime_only_keys:
            # Issue #2467 P0-1 review fix: the CURRENT side must carry an
            # ACTUAL executed PASS for this (ac, command_hash) -- the
            # baseline canonical skip (checked above, when building
            # excluded_runtime_only_keys) is delegation authorization only
            # and must never be re-required here.
            if not _is_runtime_only_current_execution_pass(norm):
                return _result(
                    overall_status="indeterminate", per_ac=[], rerun_required=True,
                    source_integrity=source_integrity, evidence_refs=evidence_refs,
                    errors=[f"runtime_only_current_execution_not_pass:{norm['ac']}"],
                )
            excluded_current_count += 1
            excluded_current_keys.add(mapping_key)
            current_order.append({"kind": "runtime_only", "norm": norm})
            continue
        current_keys.add(mapping_key)
        current_order.append({"kind": "normal", "norm": norm})

    excluded_skip_keys = excluded_pr_review_only_keys | excluded_runtime_only_keys
    if excluded_current_keys != excluded_skip_keys:
        missing_pr_review_only = excluded_pr_review_only_keys - excluded_current_keys
        missing_runtime_only = excluded_runtime_only_keys - excluded_current_keys
        if missing_runtime_only and not missing_pr_review_only:
            coverage_error = "runtime_only_coverage_mismatch"
        elif missing_pr_review_only and not missing_runtime_only:
            coverage_error = "pr_review_only_coverage_mismatch"
        else:
            coverage_error = "skipped_scope_coverage_mismatch"
        return _result(
            overall_status="indeterminate", per_ac=[], rerun_required=True,
            source_integrity=source_integrity, evidence_refs=evidence_refs,
            errors=[coverage_error],
        )

    if excluded_pr_review_only_keys:
        if independent_pr_review_only:
            binding_error = _pr_review_only_current_head_binding_error(
                contract_snapshot=contract_snapshot,
                current_vc_result=current_vc_result,
                diff_summary=diff_summary,
                changed_paths=changed_paths,
                changed_paths_present=changed_paths_present,
                allowed_paths=normalized_allowed,
                expected_issue_number=expected_issue_number,
                expected_pr_number=expected_pr_number,
                allow_delegated_nonpass=bool(delegated_nonpass_keys),
                delegated_keys=frozenset(delegated_nonpass_keys),
            )
            if binding_error is None:
                binding_error = _pr_review_only_raw_report_error(test_verdict)
        else:
            binding_error = _test_verdict_binding_error(
                test_verdict,
                contract_snapshot=contract_snapshot,
                current_vc_result=current_vc_result,
                diff_summary=diff_summary,
                expected_keys=set(baseline_by_key) | excluded_pr_review_only_keys,
                require_producer_receipt=effective_require_producer_receipt,
            )
        if binding_error is not None:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[binding_error],
            )

    if excluded_runtime_only_keys:
        runtime_only_binding_error = _runtime_only_current_head_binding_error(
            contract_snapshot=contract_snapshot,
            current_vc_result=current_vc_result,
            diff_summary=diff_summary,
            changed_paths=changed_paths,
            changed_paths_present=changed_paths_present,
            allowed_paths=normalized_allowed,
            expected_issue_number=expected_issue_number,
            expected_pr_number=expected_pr_number,
            allow_delegated_nonpass=bool(delegated_nonpass_keys),
            delegated_keys=frozenset(delegated_nonpass_keys),
        )
        if runtime_only_binding_error is not None:
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[runtime_only_binding_error],
            )

    current_pass_certified = _current_pass_envelope_is_certified(
        contract_snapshot,
        current_vc_result,
        diff_summary,
        changed_paths,
        changed_paths_present,
        normalized_allowed,
        allow_delegated_nonpass_aggregate=bool(delegated_nonpass_keys),
        delegated_keys=frozenset(delegated_nonpass_keys),
    )
    # Issue #2467 P0-2 review fix: build per_ac by walking `current_order` in
    # its ORIGINAL (Issue declaration) order, including resolved
    # pr_review_only / runtime_only entries inline at their own position --
    # never re-sorted and never dropped from per_ac when mixed with ordinary
    # regression-gate ACs (evaluate_step4_vc_gate()'s ordered command-hash
    # binding depends on per_ac reflecting the live Issue's literal
    # Verification Commands order).
    if not current_order:
        return _result(
            overall_status="indeterminate", per_ac=[], rerun_required=True,
            source_integrity=source_integrity, evidence_refs=evidence_refs,
            errors=["empty_current_results_without_pass_signal"],
        )

    if current_keys != set(baseline_by_key):
        return _result(
            overall_status="indeterminate", per_ac=[], rerun_required=True,
            source_integrity=source_integrity, evidence_refs=evidence_refs,
            errors=["baseline_current_mapping_mismatch"],
        )

    # Issue #2467 review fix scope note: `has_normal_current` distinguishes a
    # skip-only current payload from one mixed with ordinary regression-gate
    # ACs. pr_review_only's existing non-regression precedent (Issue #1540 /
    # PR #1544) is preserved exactly as-is here: a pr_review_only skip is
    # only surfaced in per_ac when it is the ONLY current evidence (no
    # ordinary ACs to compare against); when mixed with ordinary ACs it stays
    # excluded from the regression comparison per_ac list, matching prior
    # behavior. Only the NEW runtime_only handling (below) always includes
    # the resolved AC in per_ac -- Issue #2467 does not extend the
    # pr_review_only authorized scope (Out of Scope).
    has_normal_current = bool(current_keys)
    if not has_normal_current and not current_pass_certified:
        return _result(
            overall_status="indeterminate", per_ac=[], rerun_required=True,
            source_integrity=source_integrity, evidence_refs=evidence_refs,
            errors=["empty_current_results_without_pass_signal"],
        )

    per_ac: list[dict[str, Any]] = []
    for entry in current_order:
        norm = entry["norm"]
        if entry["kind"] == "pr_review_only":
            if independent_pr_review_only and (norm["ac"], norm["command_hash"]) in delegated_nonpass_keys:
                # Issue #2916: the executed non-pass fact is kept verbatim in the
                # existing ``failure_keys`` field. The entry is NOT a pass: it stays
                # ``indeterminate`` / ``blocking`` (AC not achieved) and only
                # marks the item as one the reviewer must judge.
                per_ac.append(
                    {
                        "ac": norm["ac"],
                        "status": "indeterminate",
                        "blocking": True,
                        "command_hash": norm["command_hash"],
                        "failure_keys": _encode_nonpass_facts(_nonpass_facts_of(norm)),
                        "reason_code": REASON_PR_REVIEW_ONLY_NONPASS_DELEGATED,
                        "summary": (
                            "pr_review_only current-head execution is non-pass; the recorded "
                            "facts are delegated to pr-reviewer (not AC achievement)"
                        ),
                    }
                )
                continue
            if independent_pr_review_only:
                # Issue #2912: the independent route ALWAYS keeps the resolved
                # entry at its Issue declaration position (like runtime_only),
                # so per_ac matches --expected-command-hashes-file in order
                # even for ordinary + pr_review_only + runtime_only scopes.
                per_ac.append(
                    {
                        "ac": norm["ac"],
                        "status": "pass",
                        "blocking": False,
                        "command_hash": norm["command_hash"],
                        "failure_keys": [],
                        "reason_code": "pr_review_only_runtime_evidence_pass",
                        "summary": "pr_review_only scope is covered by independent current-head executed PASS evidence",
                    }
                )
                continue
            if has_normal_current:
                continue
            per_ac.append(
                {
                    "ac": norm["ac"],
                    "status": "pass",
                    "blocking": False,
                    "command_hash": norm["command_hash"],
                    "failure_keys": [],
                    "reason_code": "pr_review_only_runtime_evidence_pass",
                    "summary": "Producer-authorized skip is covered by v2 runtime evidence",
                }
            )
            continue
        if entry["kind"] == "runtime_only":
            per_ac.append(
                {
                    "ac": norm["ac"],
                    "status": "pass",
                    "blocking": False,
                    "command_hash": norm["command_hash"],
                    "failure_keys": [],
                    "reason_code": "runtime_only_current_head_binding_pass",
                    "summary": "Producer-authorized runtime_only skip is covered by current-head PASS evidence",
                }
            )
            continue

        baseline_item = baseline_by_key[(norm["ac"], norm["command_hash"])]
        if independent_pr_review_only and norm["exit_code"] == 0 and norm["status"] != "pass":
            # Issue #2912 fix_delta (PR #2924 REQUEST_CHANGES P1): on the independent
            # route an ordinary row whose OWN status is not "pass" (skip / fail / ...)
            # is never promoted to PASS merely because exit_code == 0.
            return _result(
                overall_status="indeterminate", per_ac=[], rerun_required=True,
                source_integrity=source_integrity, evidence_refs=evidence_refs,
                errors=[f"pr_review_only_ordinary_current_status_not_pass:{norm['ac']}"],
            )
        if norm["exit_code"] == 0:
            if norm["failure_keys_present"]:
                status, blocking, rerun_required, reason_code, summary = (
                    "indeterminate", True, True, "pass_with_failure_keys",
                    "Current PASS must not contain failure keys",
                )
            elif not current_pass_certified:
                status, blocking, rerun_required, reason_code, summary = (
                    "indeterminate", True, True, "uncertified_current_pass",
                    "Current PASS lacks complete producer-certified source integrity",
                )
            elif baseline_item["classification"] == "expected_fail":
                status, blocking, rerun_required, reason_code, summary = (
                    "pass", False, False, "expected_fail_resolved_on_current_head",
                    "Expected baseline failure resolved by certified current-head PASS",
                )
            else:
                status, blocking, rerun_required, reason_code, summary = (
                    "pass", False, False, "expected_pass_still_passes",
                    "Expected baseline PASS remains a certified current-head PASS",
                )
        else:
            status, blocking, rerun_required, reason_code, summary = _classify_item(
            item=norm,
            baseline_signatures=baseline_signatures,
            baseline_failure_index=baseline_failure_index,
            changed_paths=changed_paths,
            allowed_paths=normalized_allowed,
            source_integrity=source_integrity,
        )
        entry_dict = {
            "ac": norm["ac"],
            "status": status,
            "blocking": blocking,
            "command_hash": norm["command_hash"],
            "failure_keys": norm["failure_keys"],
            "reason_code": reason_code,
            "summary": summary,
        }
        if "command_hash_note" in norm:
            entry_dict["command_hash_note"] = norm["command_hash_note"]
        per_ac.append(entry_dict)

    if not per_ac:
        return _result(
            overall_status="indeterminate",
            per_ac=[],
            rerun_required=True,
            source_integrity=source_integrity,
            evidence_refs=evidence_refs,
            errors=["empty_current_results_without_pass_signal"],
        )

    highest = max(
        (entry["status"] for entry in per_ac),
        key=lambda status: STATUS_PRIORITY.get(status, STATUS_PRIORITY["indeterminate"]),
    )
    # Issue #2916: a delegated non-pass entry is judged by the reviewer, so a
    # re-run of Step 2 cannot change it (it never sets rerun_required itself).
    rerun_required = any(
        entry["status"] in {"indeterminate", "environment_blocked"}
        and entry.get("reason_code") != REASON_PR_REVIEW_ONLY_NONPASS_DELEGATED
        for entry in per_ac
    )

    if not source_integrity["evidence_complete"] and highest in {"pre_existing_fail", "out_of_scope_fail"}:
        highest = "indeterminate"
        for entry in per_ac:
            if entry["status"] in {"pre_existing_fail", "out_of_scope_fail"}:
                entry["status"] = "indeterminate"
                entry["blocking"] = True
                entry["reason_code"] = "incomplete_evidence"
                entry["summary"] = "Status downgraded due to missing evidence"
        rerun_required = True

    return _result(
        overall_status=highest,
        per_ac=per_ac,
        rerun_required=rerun_required,
        source_integrity=source_integrity,
        evidence_refs=evidence_refs,
        errors=[],
    )


def _step4_command_hashes(adjudication_result: dict[str, Any]) -> list[str] | None:
    """Return the command hashes covered by a VC_ADJUDICATION_RESULT_V1, in the
    original per_ac order (Issue #88 fix_delta Blocker 3: ordered, not sorted --
    sorting here would make a binding-tuple whose Verification Commands were
    merely reordered look identical to the original ordering)."""
    per_ac = adjudication_result.get("per_ac")
    if not isinstance(per_ac, list) or not per_ac:
        return None
    hashes: list[str] = []
    for entry in per_ac:
        if not isinstance(entry, dict):
            return None
        command_hash = entry.get("command_hash")
        if not isinstance(command_hash, str) or not command_hash:
            return None
        hashes.append(command_hash)
    return hashes


def evaluate_step4_vc_gate(
    adjudication_result: Any,
    *,
    expected_head_sha: str,
    expected_contract_body_sha256: str,
    expected_command_hashes: list[str],
) -> dict[str, Any]:
    """Decide whether pr-reviewer may be invoked (Issue #88 current-head gate).

    Re-derives a fail-closed boolean decision from an existing
    VC_ADJUDICATION_RESULT_V1 payload produced by adjudicate_vc_result() and
    an explicit "expected" binding triple (current-head SHA, current live
    Issue body SHA256, and the ordered literal Verification Command hashes
    Step 4 is about to evaluate). This reuses the existing adjudication
    result rather than re-classifying VC failures (Issue #88 Required
    Design #2) and holds no state of its own -- no new persistent ledger,
    authorization packet, publisher, or hook is introduced by this function
    (Issue #88 Required Design #9 / Out of Scope). We do not add a new
    persistent ledger anywhere in this module; any future persistent ledger
    for this gate would require a separate Issue.

    Returns a mapping with keys:
      - invoke_pr_reviewer: bool
      - reason_code: str | None -- one of "adjudication_missing_or_malformed",
        "adjudication_blocking_true", "adjudication_ac_not_resolved",
        "head_mismatch", "body_mismatch", "command_mismatch", or None when
        invoke_pr_reviewer is True.
    """
    if not isinstance(adjudication_result, dict) or adjudication_result.get("schema") != SCHEMA_NAME:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}

    per_ac = adjudication_result.get("per_ac")
    source_integrity = adjudication_result.get("source_integrity")
    if not isinstance(per_ac, list) or not per_ac or not isinstance(source_integrity, dict):
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}

    # Issue #88 fix_delta Warning 5: minimal additional malformed checks so a
    # stale/incomplete/rerun-pending adjudication cannot slip through just
    # because "blocking" happens to be False.
    if adjudication_result.get("schema_version") != SCHEMA_VERSION:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if adjudication_result.get("errors"):
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if adjudication_result.get("rerun_required") is not False:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if source_integrity.get("evidence_complete") is not True:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}
    if source_integrity.get("evidence_fresh") is not True:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_missing_or_malformed"}

    # Issue #2916: an executed non-pass pr_review_only item delegated to the
    # reviewer is the ONLY shape allowed to coexist with ``blocking: true``. It
    # permits a reviewer DISPATCH only: the persisted adjudication keeps
    # ``overall_status: indeterminate`` / ``blocking: true`` (the AC is not
    # achieved) and terminal approval still needs step5_terminal_gate().
    has_delegated = any(
        isinstance(entry, dict) and entry.get("reason_code") == REASON_PR_REVIEW_ONLY_NONPASS_DELEGATED
        for entry in per_ac
    )
    if has_delegated:
        if (
            adjudication_result.get("overall_status") != "indeterminate"
            or adjudication_result.get("blocking") is not True
        ):
            return {"invoke_pr_reviewer": False, "reason_code": "adjudication_ac_not_resolved"}
    elif adjudication_result.get("blocking") is not False:
        return {"invoke_pr_reviewer": False, "reason_code": "adjudication_blocking_true"}

    for entry in per_ac:
        if isinstance(entry, dict) and entry.get("reason_code") == REASON_PR_REVIEW_ONLY_NONPASS_DELEGATED:
            if not _is_valid_delegated_nonpass_entry(entry):
                return {"invoke_pr_reviewer": False, "reason_code": "adjudication_ac_not_resolved"}
            continue
        if not isinstance(entry, dict) or entry.get("status") not in {
            "pass",
            "pre_existing_fail",
            "out_of_scope_fail",
        }:
            return {"invoke_pr_reviewer": False, "reason_code": "adjudication_ac_not_resolved"}
        if has_delegated and entry.get("blocking") is not False:
            return {"invoke_pr_reviewer": False, "reason_code": "adjudication_blocking_true"}

    if source_integrity.get("head_sha") != expected_head_sha:
        return {"invoke_pr_reviewer": False, "reason_code": "head_mismatch"}

    if source_integrity.get("contract_body_sha256") != expected_contract_body_sha256:
        return {"invoke_pr_reviewer": False, "reason_code": "body_mismatch"}

    # Issue #88 fix_delta Blocker 3: compare in literal order, not sorted --
    # a reordering of the same command set must be treated as a binding
    # mismatch (the Verification Commands block changed even if its set of
    # SHA256 hashes did not).
    observed_hashes = _step4_command_hashes(adjudication_result)
    if observed_hashes is None or observed_hashes != list(expected_command_hashes):
        return {"invoke_pr_reviewer": False, "reason_code": "command_mismatch"}

    return {"invoke_pr_reviewer": True, "reason_code": None}


def step4_binding_key(
    *, head_sha: str, contract_body_sha256: str, command_hashes: list[str]
) -> str:
    """Identity of a Step 2/Step 4 binding tuple (Issue #88 AC5).

    Command hashes are kept in their literal order (Issue #88 fix_delta
    Blocker 3 -- a reordering of the same command set is a different
    binding). The key is a plain string so it round-trips through
    LOOP_STATE YAML (Issue #88 fix_delta Blocker 2 -- superseding
    Step4AdjudicationCache, an in-memory-only object that could not survive
    across separate CLI invocations of this script).
    """
    payload = {
        "head_sha": head_sha,
        "contract_body_sha256": contract_body_sha256,
        "command_hashes": list(command_hashes),
    }
    return _sha256(_canonical_json(payload))


def step4_gate_from_loop_state(
    loop_state: dict[str, Any],
    *,
    expected_head_sha: str,
    expected_contract_body_sha256: str,
    expected_command_hashes: list[str],
) -> dict[str, Any]:
    """LOOP_STATE-based replacement for Step4AdjudicationCache.get_or_run()
    (Issue #88 fix_delta Blocker 2).

    Looks up a previously persisted VC_ADJUDICATION_RESULT_V1 for the current
    binding tuple in ``loop_state["vc_adjudication"]`` -- a plain
    YAML-serializable mapping written by Step 2 via
    ``step4_persist_vc_adjudication()``, not a Python-process-scoped cache
    object -- and re-validates it through ``evaluate_step4_vc_gate()`` so a
    stale/invalid stored entry is never reused as-is.

    Returns the same mapping shape as ``evaluate_step4_vc_gate()`` plus a
    ``"reused"`` boolean: True when a stored adjudication was found for this
    binding tuple (regardless of whether it still opens the gate), False
    when Step 2 must run test-runner again because no entry exists yet.
    """
    entries = loop_state.get("vc_adjudication")
    if not isinstance(entries, dict):
        return {
            "invoke_pr_reviewer": False,
            "reason_code": "adjudication_missing_or_malformed",
            "reused": False,
        }
    binding_key = step4_binding_key(
        head_sha=expected_head_sha,
        contract_body_sha256=expected_contract_body_sha256,
        command_hashes=expected_command_hashes,
    )
    stored = entries.get(binding_key)
    if stored is None:
        return {
            "invoke_pr_reviewer": False,
            "reason_code": "adjudication_missing_or_malformed",
            "reused": False,
        }
    decision = evaluate_step4_vc_gate(
        stored,
        expected_head_sha=expected_head_sha,
        expected_contract_body_sha256=expected_contract_body_sha256,
        expected_command_hashes=expected_command_hashes,
    )
    decision["reused"] = True
    return decision


def step4_persist_vc_adjudication(
    loop_state: dict[str, Any],
    *,
    head_sha: str,
    contract_body_sha256: str,
    command_hashes: list[str],
    adjudication_result: dict[str, Any],
) -> dict[str, Any]:
    """Persist a VC_ADJUDICATION_RESULT_V1 into
    ``loop_state["vc_adjudication"]`` keyed by the Step 4 binding tuple
    (Issue #88 fix_delta Blocker 2). Only a result that
    ``evaluate_step4_vc_gate()`` would accept (``invoke_pr_reviewer is
    True`` against its own asserted binding) is stored -- malformed,
    blocking, or unresolved adjudications are never written, so a later
    ``step4_gate_from_loop_state()`` lookup can only ever reuse a genuinely
    valid adjudication (Issue #88 AC5: "valid adjudication のみ再利用").
    Issue #2837 invalidation: the binding key is computed from the
    caller-supplied binding arguments, never from the result. When the new
    result does not open the gate, any existing entry stored under that same
    key is removed, so a previously persisted PASS can never be consumed as
    evidence after a canonical re-verification of the same binding was
    blocking / malformed / indeterminate. No time-based TTL or new ledger is
    introduced; entries for other binding keys are left untouched.
    Returns the (possibly modified) ``loop_state`` for convenience.
    """
    self_check = evaluate_step4_vc_gate(
        adjudication_result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=contract_body_sha256,
        expected_command_hashes=command_hashes,
    )
    binding_key = step4_binding_key(
        head_sha=head_sha,
        contract_body_sha256=contract_body_sha256,
        command_hashes=command_hashes,
    )
    if not self_check["invoke_pr_reviewer"]:
        existing = loop_state.get("vc_adjudication")
        if isinstance(existing, dict):
            existing.pop(binding_key, None)
        return loop_state
    entries = loop_state.setdefault("vc_adjudication", {})
    entries[binding_key] = adjudication_result
    return loop_state


# --- Issue #2837: production wiring of the independent-VC consumer -----------
#
# ``step4-adjudicate`` (adapt -> adjudicate -> persist -> gate in one process,
# plus the only writer of ``loop_state["dispatch"]``) and
# ``step5-terminal-gate`` (terminal approval composition point). No new schema
# family, ledger, TTL, hook, or route constant is introduced here: only the
# existing ``vc_adjudication`` mapping plus the single ``dispatch`` key
# (``binding_key`` / ``seq``) is persisted, and the route decision itself is
# produced by the existing public wrapper in route_loop_verdict_v2.py.

REASON_VC_GATE_BLOCKING = "vc_gate_blocking"
REASON_DISPATCH_SEQ_MISMATCH = "dispatch_seq_mismatch"
REASON_BINDING_CHANGED_SINCE_DISPATCH = "binding_changed_since_dispatch"

_ROUTE_MODULE_PATH = Path(__file__).resolve().parent / "route_loop_verdict_v2.py"
_ROUTE_MODULE_UNIQUE_NAME = "route_loop_verdict_v2_loaded_by_adjudicate_vc_result"
_ROUTE_MODULE: Any = None


def _load_route_module() -> Any:
    """Load route_loop_verdict_v2.py under a unique module name.

    A bare ``import route_loop_verdict_v2`` can collide with another file of
    the same name inside a shared pytest session (sys.modules cache), so the
    module is loaded by path. Only the public ``ROUTE_*`` constants and the
    public wrapper ``route_loop_verdict_v2_resolve_semantic_ambiguity()`` are
    used; ``RouteDecision`` / ``_decision`` internals are not depended on.
    """
    global _ROUTE_MODULE
    if _ROUTE_MODULE is not None:
        return _ROUTE_MODULE
    spec = importlib.util.spec_from_file_location(_ROUTE_MODULE_UNIQUE_NAME, _ROUTE_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("route_loop_verdict_v2_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ROUTE_MODULE_UNIQUE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ROUTE_MODULE_UNIQUE_NAME, None)
        raise
    _ROUTE_MODULE = module
    return module


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _dispatch_record(loop_state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the well-formed ``loop_state["dispatch"]`` mapping, else None."""
    dispatch = loop_state.get("dispatch")
    if not isinstance(dispatch, dict):
        return None
    binding_key = dispatch.get("binding_key")
    if not isinstance(binding_key, str) or not binding_key:
        return None
    if not _is_positive_int(dispatch.get("seq")):
        return None
    return dispatch


def _record_dispatch(loop_state: dict[str, Any], *, binding_key: str) -> int:
    """Advance ``loop_state["dispatch"]`` by one invoke and return the new seq."""
    previous = _dispatch_record(loop_state)
    seq = (previous["seq"] if previous is not None else 0) + 1
    loop_state["dispatch"] = {"binding_key": binding_key, "seq": seq}
    return seq


# --- Issue #2996: ensure_contract_snapshot envelope -> Step 4 handoff --------
#
# ``ensure_contract_snapshot.py --artifact-dir`` saves the whole
# CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 envelope, which is not one of the shapes
# ``_normalize_list_payload()`` accepts. ``resolve_step4_contract_snapshot()``
# resolves such an envelope ONCE, inside the ``step4-adjudicate`` input
# pre-processing, into a CONTRACT_REVIEW_RESULT_V1-shaped canonical object
# (top-level ``status`` / ``body_sha256`` / ``checks.vc_preflight.
# classifications``). That single object is what classification, source
# integrity and the current-head binding all read, so none of them can see a
# different view of the snapshot. Any failure returns ``(None, errors)``: the
# caller passes NO partial snapshot, so the normal adjudication / persist path
# still runs and invalidates a PASS stored for the same binding (Issue #2837).
# No new schema, ledger, state writer or approval layer is introduced, and the
# producer's published schema / exit codes are consumed as-is.

ENSURE_ENVELOPE_SCHEMA = "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1"
ENSURE_ONCE_RESULT_SCHEMA = "CONTRACT_REVIEW_ONCE_RESULT_V1"
CANONICAL_CONTRACT_REVIEW_SCHEMA = "CONTRACT_REVIEW_RESULT_V1"
# The only (status -> producer exit code) pairs that may be handed off. A
# ``human_judgment`` envelope also exits 20 but is never eligible, so it is
# intentionally absent from this table.
_ENVELOPE_HANDOFF_EXIT_CODES = {"ok": 0, "dry_run_would_post": 20}
_ENVELOPE_OK_SOURCES = frozenset({"existing_go", "materialized_go"})
# A real process exit code is tiny; bound the digit count well below the
# interpreter int/str conversion limit so int() can never raise.
_PRODUCER_EXIT_CODE_MAX_DIGITS = 9
_CONTRACT_COMMENT_URL_RE = re.compile(
    r"\Ahttps://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([0-9]+)#issuecomment-([0-9]+)\Z"
)
_REPO_SLUG_RE = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_CONTRACT_PARSER_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "contract_review_result_parser.py"
)
_CONTRACT_PARSER_UNIQUE_NAME = "contract_review_result_parser_loaded_by_adjudicate_vc_result"
_CONTRACT_PARSER: Any = None


def _load_contract_result_parser() -> Any:
    """Load the shared trusted-comment parser by path (never a bare import: a
    same-named module elsewhere in a shared pytest session would collide)."""
    global _CONTRACT_PARSER
    if _CONTRACT_PARSER is not None:
        return _CONTRACT_PARSER
    spec = importlib.util.spec_from_file_location(_CONTRACT_PARSER_UNIQUE_NAME, _CONTRACT_PARSER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("contract_review_result_parser_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CONTRACT_PARSER_UNIQUE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_CONTRACT_PARSER_UNIQUE_NAME, None)
        raise
    _CONTRACT_PARSER = module
    return module


def _parse_producer_exit_code(raw: Any) -> tuple[int | None, str | None]:
    """Return ``(exit_code, None)`` for a plain non-negative integer, else
    ``(None, reason)``. bool / float / signed / empty values are rejected so a
    malformed code can never be coerced into a match."""
    if raw is None:
        return None, "producer_exit_code_missing"
    if isinstance(raw, bool):
        return None, "producer_exit_code_not_integer"
    if isinstance(raw, int):
        return (raw, None) if raw >= 0 else (None, "producer_exit_code_not_integer")
    if isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw):
        if len(raw) > _PRODUCER_EXIT_CODE_MAX_DIGITS:
            # A huge decimal string would raise ValueError in int() (the
            # interpreter's int/str digit limit) -- reject it structurally.
            return None, "producer_exit_code_not_integer"
        try:
            return int(raw), None
        except ValueError:
            return None, "producer_exit_code_not_integer"
    return None, "producer_exit_code_not_integer"


def _canonical_contract_review_result(
    *, body_sha256: str, classifications: list[Any]
) -> dict[str, Any]:
    """The single CONTRACT_REVIEW_RESULT_V1-shaped object every later stage reads."""
    return {
        "schema": CANONICAL_CONTRACT_REVIEW_SCHEMA,
        "status": "go",
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": classifications}},
    }


def _resolve_dry_run_candidate(
    envelope: dict[str, Any], *, expected_contract_body_sha256: str | None
) -> tuple[dict[str, Any] | None, list[str]]:
    """(1) ``dry_run_would_post`` + ``materialized_go``: the nested candidate
    result is the evidence authority (nothing was posted, so there is no
    comment). It is a candidate only -- never a published trusted snapshot."""
    source = envelope.get("source")
    if not isinstance(source, str):
        return None, [f"envelope_source_not_string:{type(source).__name__}"]
    if source != "materialized_go":
        return None, [f"envelope_source_not_allowed:dry_run_would_post:{source}"]
    if envelope.get("contract_snapshot_url") is not None:
        return None, ["envelope_dry_run_contract_snapshot_url_not_null"]
    nested = envelope.get("contract_review_once_result")
    if not isinstance(nested, dict) or nested.get("schema") != ENSURE_ONCE_RESULT_SCHEMA:
        return None, ["envelope_nested_result_missing_or_wrong_schema"]
    if nested.get("status") != "go":
        return None, [f"envelope_nested_result_not_go:{nested.get('status')}"]
    body_sha256 = nested.get("body_sha256")
    if not _is_nonempty_string(body_sha256):
        return None, ["envelope_nested_body_sha256_missing"]
    if body_sha256 != envelope.get("body_sha256_at_check"):
        return None, ["envelope_nested_body_sha256_binding_mismatch"]
    if nested.get("issue_number") != envelope.get("issue_number"):
        return None, ["envelope_nested_issue_number_mismatch"]
    if expected_contract_body_sha256 is not None and body_sha256 != expected_contract_body_sha256:
        return None, ["snapshot_body_sha256_mismatch"]
    classifications = nested.get("vc_preflight_classifications")
    if not isinstance(classifications, list):
        return None, ["envelope_nested_vc_preflight_classifications_missing"]
    return _canonical_contract_review_result(
        body_sha256=body_sha256, classifications=classifications
    ), []


def _resolve_posted_snapshot_comment(
    envelope: dict[str, Any],
    *,
    repo: str | None,
    expected_issue_number: Any,
    expected_contract_body_sha256: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """(2) ``ok`` + ``existing_go`` / (3) ``ok`` + ``materialized_go``: the
    ``contract_snapshot_url`` comment is the evidence authority and is
    re-verified with the shared trusted-comment parser. The envelope's nested
    result is never the authority here (it is null for ``existing_go``)."""
    source = envelope.get("source")
    if not isinstance(source, str):
        return None, [f"envelope_source_not_string:{type(source).__name__}"]
    if source not in _ENVELOPE_OK_SOURCES:
        return None, [f"envelope_source_not_allowed:ok:{source}"]
    if not isinstance(repo, str) or _REPO_SLUG_RE.fullmatch(repo) is None:
        return None, ["repo_required_for_ok_snapshot"]
    if not _is_positive_int(expected_issue_number):
        return None, ["expected_issue_number_required_for_ok_snapshot"]
    envelope_repo = envelope.get("repo")
    if not isinstance(envelope_repo, str) or envelope_repo.casefold() != repo.casefold():
        return None, ["envelope_repo_mismatch"]
    if envelope.get("issue_number") != expected_issue_number:
        return None, ["envelope_issue_number_mismatch"]
    url = envelope.get("contract_snapshot_url")
    match = _CONTRACT_COMMENT_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        return None, ["contract_snapshot_url_missing_or_malformed"]
    url_owner_repo = f"{match.group(1)}/{match.group(2)}"
    if url_owner_repo.casefold() != repo.casefold():
        return None, ["contract_snapshot_url_repo_mismatch"]
    if int(match.group(3)) != expected_issue_number:
        return None, ["contract_snapshot_url_issue_mismatch"]
    url_comment_id = int(match.group(4))

    try:
        parser = _load_contract_result_parser()
        comments, fetch_error = parser.fetch_issue_comments(expected_issue_number, repo)
        if fetch_error:
            return None, [f"contract_snapshot_comments_fetch_failed:{fetch_error}"]
        results = parser.parse_contract_review_results(
            comments, expected_issue_url=f"https://github.com/{repo}/issues/{expected_issue_number}"
        )
        # #3012: a trusted ``go`` that is not fingerprint-ready is never
        # adoptable and must not become "latest" ahead of an earlier trusted
        # blocked (same ``adoptable`` semantics as
        # verify_snapshot_authority_postcondition). When no adoptable trusted
        # result exists at all, keep the unfiltered latest so the existing
        # ``contract_snapshot_no_fingerprint_ready_go`` classification holds.
        adoptable = [
            item
            for item in results
            if item.get("status") != "go" or item.get("is_fingerprint_ready") is True
        ]
        latest = parser.find_latest_result(adoptable, trusted_only=True) or parser.find_latest_result(
            results, trusted_only=True
        )
        latest_go = parser.find_latest_go(results, trusted_only=True, fingerprint_ready_only=True)
    except Exception as exc:  # fail closed on any parser / transport failure
        return None, [f"contract_snapshot_resolution_error:{type(exc).__name__}"]

    # Same authoritative-result precedence as the producer
    # (ensure_contract_snapshot.py): a latest trusted ``blocked`` wins; otherwise
    # the authority is the latest trusted AND fingerprint-ready ``go``. A newer
    # fingerprint-unready (provisional / orphan) ``go`` is not an authoritative
    # GO and must not invalidate an existing fingerprint-ready GO, so the latest
    # trusted result's comment id is deliberately NOT compared with the URL.
    if not isinstance(latest, dict):
        return None, ["contract_snapshot_no_trusted_result"]
    latest_status = latest.get("status")
    if latest_status == "blocked":
        return None, ["contract_snapshot_latest_trusted_result_blocked"]
    if latest_status != "go":
        # Unknown status: stricter than the producer (fail closed).
        return None, [f"contract_snapshot_latest_trusted_result_status_unsupported:{str(latest_status)[:40]}"]
    if not isinstance(latest_go, dict):
        return None, ["contract_snapshot_no_fingerprint_ready_go"]
    if latest_go.get("comment_id") != url_comment_id:
        return None, ["contract_snapshot_comment_not_latest_fingerprint_ready_go"]
    inner = latest_go.get("inner")
    if not isinstance(inner, dict):
        return None, ["contract_snapshot_inner_result_missing"]
    body_sha256 = inner.get("body_sha256")
    if not _is_nonempty_string(body_sha256):
        return None, ["contract_snapshot_body_sha256_missing"]
    if expected_contract_body_sha256 is not None and body_sha256 != expected_contract_body_sha256:
        return None, ["snapshot_body_sha256_mismatch"]
    checks = inner.get("checks")
    vc_preflight = checks.get("vc_preflight") if isinstance(checks, dict) else None
    classifications = vc_preflight.get("classifications") if isinstance(vc_preflight, dict) else None
    if not isinstance(classifications, list):
        return None, ["contract_snapshot_vc_preflight_classifications_missing"]
    return _canonical_contract_review_result(
        body_sha256=body_sha256, classifications=classifications
    ), []


def resolve_step4_contract_snapshot(
    snapshot: Any,
    *,
    producer_exit_code: Any = None,
    repo: str | None = None,
    expected_issue_number: Any = None,
    expected_contract_body_sha256: str | None = None,
) -> tuple[Any, list[str]]:
    """Resolve a ``--contract-snapshot-file`` payload for ``step4-adjudicate``.

    A non-envelope snapshot is returned unchanged (existing behaviour) and may
    not be combined with ``producer_exit_code``. A
    CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 envelope requires ``producer_exit_code``
    and is resolved per the allowed ``(status, source)`` table; every other
    combination fails closed with ``(None, errors)`` -- never a partial object.
    """
    is_envelope = isinstance(snapshot, dict) and snapshot.get("schema") == ENSURE_ENVELOPE_SCHEMA
    if not is_envelope:
        if producer_exit_code is not None:
            return None, ["producer_exit_code_requires_envelope_snapshot"]
        return snapshot, []

    exit_code, exit_code_error = _parse_producer_exit_code(producer_exit_code)
    if exit_code_error is not None:
        return None, [exit_code_error]
    status = snapshot.get("status")
    if not isinstance(status, str):
        # list / dict / number would raise TypeError (unhashable) in the
        # membership test below; reject it as a structured input error.
        return None, [f"envelope_status_not_string:{type(status).__name__}"]
    if status not in _ENVELOPE_HANDOFF_EXIT_CODES:
        # human_judgment (also exit 20), blocked_needs_refinement,
        # runtime_error, stale_or_conflicting_snapshot,
        # controlled_publisher_binding_failed and any unknown status.
        return None, [f"producer_status_not_handoff_eligible:{status}"]
    if exit_code != _ENVELOPE_HANDOFF_EXIT_CODES[status]:
        return None, [f"producer_exit_code_mismatch:status={status}:exit_code={exit_code}"]
    if not _is_positive_int(snapshot.get("issue_number")):
        return None, ["envelope_issue_number_missing"]
    if _is_positive_int(expected_issue_number) and snapshot["issue_number"] != expected_issue_number:
        return None, ["envelope_issue_number_mismatch"]

    if status == "dry_run_would_post":
        return _resolve_dry_run_candidate(
            snapshot, expected_contract_body_sha256=expected_contract_body_sha256
        )
    return _resolve_posted_snapshot_comment(
        snapshot,
        repo=repo,
        expected_issue_number=expected_issue_number,
        expected_contract_body_sha256=expected_contract_body_sha256,
    )


def step4_adjudicate(
    loop_state: dict[str, Any],
    *,
    expected_head_sha: str,
    expected_contract_body_sha256: str,
    expected_command_hashes: list[str],
    test_verdict: Any = None,
    test_verdict_errors: list[str] | None = None,
    contract_snapshot: Any = None,
    contract_snapshot_errors: list[str] | None = None,
    diff_summary: Any = None,
    diff_summary_errors: list[str] | None = None,
    allowed_paths: list[str] | None = None,
    allowed_paths_errors: list[str] | None = None,
    expected_issue_number: Any = None,
    expected_pr_number: Any = None,
    require_producer_receipt: bool = False,
    reuse_stored: bool = False,
    delegate_pr_review_only_nonpass: bool = False,
) -> tuple[int, dict[str, Any]]:
    """adapt -> adjudicate -> persist -> gate in a single process (Issue #2837).

    Mutates ``loop_state`` in place and returns ``(exit_code, payload)``:
    0 = invoke (a reviewer dispatch is permitted and recorded under
    ``loop_state["dispatch"]``), 1 = rerun. Exit code 2 (malformed CLI input)
    is decided by the caller before this function runs.

    The binding key used for persistence / invalidation is computed from the
    caller-supplied expected arguments, not from the result, so every
    non-opening outcome (adapt errors, indeterminate adjudication, missing
    test-verdict, blocking result) removes an existing PASS stored under the
    same key. The full (pre-compaction) adjudication result is handed to
    persist; no intermediate file is involved.

    ``reuse_stored=True`` skips adapt / adjudicate / invalidation entirely and
    only re-evaluates the stored PASS for the caller-supplied binding.
    """
    expected_hashes = list(expected_command_hashes)
    binding_key = step4_binding_key(
        head_sha=expected_head_sha,
        contract_body_sha256=expected_contract_body_sha256,
        command_hashes=expected_hashes,
    )

    def _payload(decision: dict[str, Any], *, seq: int | None, summary: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(decision)
        payload["binding_key"] = binding_key
        payload["seq"] = seq
        if summary is not None:
            payload["adjudication"] = summary
        return payload

    if reuse_stored:
        decision = step4_gate_from_loop_state(
            loop_state,
            expected_head_sha=expected_head_sha,
            expected_contract_body_sha256=expected_contract_body_sha256,
            expected_command_hashes=expected_hashes,
        )
        if not decision["invoke_pr_reviewer"]:
            return 1, _payload(decision, seq=None, summary=None)
        seq = _record_dispatch(loop_state, binding_key=binding_key)
        return 0, _payload(decision, seq=seq, summary=None)

    adjudication: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    input_errors: list[str] = []
    if test_verdict_errors:
        # Missing / unreadable test-runner report: no adapt, no adjudication.
        input_errors = list(test_verdict_errors)
        summary = {"overall_status": "indeterminate", "blocking": True, "errors": input_errors}
    else:
        verdict_payload = test_verdict
        if isinstance(verdict_payload, dict) and isinstance(verdict_payload.get("TEST_VERDICT"), dict):
            verdict_payload = verdict_payload["TEST_VERDICT"]
        converted, adapt_errors = adapt_test_verdict_to_current_vc_result(test_verdict)
        if converted is None or adapt_errors:
            input_errors = ["adapt_failed", *(adapt_errors or [])]
            summary = {"overall_status": "indeterminate", "blocking": True, "errors": input_errors}
        else:
            adjudication = adjudicate_vc_result(
                contract_snapshot=contract_snapshot,
                current_vc_result=converted,
                diff_summary=diff_summary,
                allowed_paths=allowed_paths,
                test_verdict=verdict_payload,
                require_producer_receipt=require_producer_receipt,
                expected_issue_number=expected_issue_number,
                expected_pr_number=expected_pr_number,
                delegate_pr_review_only_nonpass=delegate_pr_review_only_nonpass,
            )
            for extra in (
                contract_snapshot_errors,
                diff_summary_errors,
                allowed_paths_errors,
            ):
                adjudication["errors"].extend(extra or [])
            if adjudication["errors"]:
                adjudication["overall_status"] = "indeterminate"
                adjudication["blocking"] = True
                adjudication["rerun_required"] = True
            summary = {
                "overall_status": adjudication["overall_status"],
                "blocking": adjudication["blocking"],
                "errors": list(adjudication["errors"]),
            }
            delegated_acs = [
                entry["ac"]
                for entry in adjudication["per_ac"]
                if entry.get("reason_code") == REASON_PR_REVIEW_ONLY_NONPASS_DELEGATED
            ]
            if delegated_acs and not adjudication["errors"]:
                # Issue #2916: tell the root that a reviewer dispatch (if opened)
                # is a delegation of recorded non-pass facts, not an AC pass.
                summary["pr_review_only_nonpass_delegated"] = delegated_acs

    step4_persist_vc_adjudication(
        loop_state,
        head_sha=expected_head_sha,
        contract_body_sha256=expected_contract_body_sha256,
        command_hashes=expected_hashes,
        adjudication_result=adjudication if adjudication is not None else {},
    )
    decision = step4_gate_from_loop_state(
        loop_state,
        expected_head_sha=expected_head_sha,
        expected_contract_body_sha256=expected_contract_body_sha256,
        expected_command_hashes=expected_hashes,
    )
    if not decision["invoke_pr_reviewer"]:
        return 1, _payload(decision, seq=None, summary=summary)
    seq = _record_dispatch(loop_state, binding_key=binding_key)
    return 0, _payload(decision, seq=seq, summary=summary)


def _plain_route_decision(decision: Any) -> dict[str, Any]:
    """Serialize a RouteDecision-shaped object to plain JSON."""
    return {
        "route": decision.route,
        "fail_closed": bool(decision.fail_closed),
        "reason_code": decision.reason_code,
        "selected_action": (
            json.loads(json.dumps(dict(decision.selected_action)))
            if decision.selected_action is not None
            else None
        ),
        "rerun_required": dict(decision.rerun_required),
        "errors": list(decision.errors),
    }


def _continue_loop_decision(
    reason_code: str, *, verification: bool, pr_review: bool, errors: list[str]
) -> dict[str, Any]:
    route_module = _load_route_module()
    return {
        "route": route_module.ROUTE_CONTINUE_LOOP,
        "fail_closed": False,
        "reason_code": reason_code,
        "selected_action": None,
        "rerun_required": {"verification": verification, "pr_review": pr_review},
        "errors": errors,
    }


def step5_terminal_gate(
    loop_state: dict[str, Any],
    reviewer_verdict: Any,
    live_mergeability: Any,
    *,
    expected_head_sha: str,
    expected_contract_body_sha256: str,
    expected_command_hashes: list[str],
    dispatch_seq: int,
    cwd: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Terminal approval composition point (Issue #2837).

    Returns ``(exit_code, route_decision_json)``: 0 = approved, 1 = anything
    else (the route is output as-is, or ``continue_loop`` with one of the
    gate reason codes). Evaluation order and reason_code priority:
    ``dispatch_seq_mismatch`` -> ``binding_changed_since_dispatch`` ->
    ``vc_gate_blocking``. ``binding_changed_since_dispatch`` covers both the
    dispatch binding key mismatch and ``live_mergeability["head_sha"] !=
    expected_head_sha`` (split-head). Only a route that is already ``approved`` is
    subject to these checks; any other route is passed through unchanged.
    VC is never re-run here.
    """
    route_module = _load_route_module()
    decision = route_module.route_loop_verdict_v2_resolve_semantic_ambiguity(
        reviewer_verdict,
        live_mergeability,
        cwd=cwd if cwd is not None else _REPO_ROOT,
    )
    plain = _plain_route_decision(decision)
    if plain["route"] != route_module.ROUTE_APPROVED:
        return 1, plain

    dispatch = _dispatch_record(loop_state)
    if dispatch is None:
        return 1, _continue_loop_decision(
            REASON_DISPATCH_SEQ_MISMATCH,
            verification=False,
            pr_review=True,
            errors=["dispatch_missing_or_malformed"],
        )
    if dispatch["seq"] != dispatch_seq:
        return 1, _continue_loop_decision(
            REASON_DISPATCH_SEQ_MISMATCH,
            verification=False,
            pr_review=True,
            errors=[f"dispatch_seq_mismatch:reviewer_seq={dispatch_seq!r}:current_seq={dispatch['seq']!r}"],
        )

    live_binding_key = step4_binding_key(
        head_sha=expected_head_sha,
        contract_body_sha256=expected_contract_body_sha256,
        command_hashes=list(expected_command_hashes),
    )
    if dispatch["binding_key"] != live_binding_key:
        return 1, _continue_loop_decision(
            REASON_BINDING_CHANGED_SINCE_DISPATCH,
            verification=True,
            pr_review=True,
            errors=["dispatch_binding_key_differs_from_live_binding"],
        )

    # Split-head guard (Issue #2837 fix_delta): the router approves based on
    # ``reviewer_verdict.reviewed_head_sha == live_mergeability["head_sha"]``
    # while the VC binding above is derived from ``expected_head_sha``. Without
    # this cross-check a reviewer/live pair on HEAD B could be combined with a
    # VC PASS bound to HEAD A. A non-dict ``live_mergeability`` or a missing /
    # non-string ``head_sha`` is a mismatch (fail-closed).
    live_head_sha = live_mergeability.get("head_sha") if isinstance(live_mergeability, dict) else None
    if not isinstance(live_head_sha, str) or live_head_sha != expected_head_sha:
        return 1, _continue_loop_decision(
            REASON_BINDING_CHANGED_SINCE_DISPATCH,
            verification=True,
            pr_review=True,
            errors=[
                f"live_mergeability_head_sha_differs_from_expected_head_sha:live={live_head_sha!r}:expected={expected_head_sha!r}"
            ],
        )

    gate = step4_gate_from_loop_state(
        loop_state,
        expected_head_sha=expected_head_sha,
        expected_contract_body_sha256=expected_contract_body_sha256,
        expected_command_hashes=list(expected_command_hashes),
    )
    if not gate["invoke_pr_reviewer"]:
        return 1, _continue_loop_decision(
            REASON_VC_GATE_BLOCKING,
            verification=True,
            pr_review=False,
            errors=[f"vc_gate:{gate['reason_code']}"],
        )
    return 0, plain


# --- Issue #2837 fix_delta: side-effect-free VC metadata extraction ----------
#
# ``baseline_vc_preflight.py`` is the *executor* (it classifies by running the
# Verification Commands); it is NOT parse-only, and ``--static-only`` does not
# enumerate the command hashes of a healthy body. The Step 2 / Step 4 callers
# only need the ordered ``(ac label, literal command, command_hash)`` triples
# of the live Issue body, so this thin function reuses baseline's own pure
# helpers (single normalization / AC-labeling / hash authority) without ever
# starting a subprocess. Classification authority stays with the contract
# snapshot; no new parser or schema family is introduced.

_BASELINE_MODULE_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
)
_BASELINE_MODULE_UNIQUE_NAME = "baseline_vc_preflight_loaded_by_adjudicate_vc_result"
_BASELINE_MODULE: Any = None


def _load_baseline_module() -> Any:
    """Load baseline_vc_preflight.py under a unique module name (see
    ``_load_route_module`` for why a bare import is avoided). Only its pure
    helpers are used; none of its executor entrypoints is called."""
    global _BASELINE_MODULE
    if _BASELINE_MODULE is not None:
        return _BASELINE_MODULE
    spec = importlib.util.spec_from_file_location(_BASELINE_MODULE_UNIQUE_NAME, _BASELINE_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("baseline_vc_preflight_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_BASELINE_MODULE_UNIQUE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_BASELINE_MODULE_UNIQUE_NAME, None)
        raise
    _BASELINE_MODULE = module
    return module


def extract_vc_metadata(body: str) -> tuple[int, dict[str, Any]]:
    """Extract ordered VC metadata from a live Issue body without running anything.

    Returns ``(exit_code, payload)``. ``payload`` is
    ``{status, commands:[{ac,line,raw_command,command_hash}],
    command_hashes:[...], static_errors:[...], errors:[...]}``.
    ``ac`` / ``line`` / ``command_hash`` follow ``baseline_vc_preflight.py``'s
    ``results[]`` exactly (``AC_UNKNOWN`` for an unlabeled command,
    numerically sorted comma-joined multi-AC labels, block-relative ``line``,
    ``sha256:<hex>`` hash; declaration order is preserved).

    Exit codes mirror baseline's extraction failures: 0 = ok, 2 = no
    ``## Verification Commands`` section / a ``non_dollar_command`` static
    error (baseline rejects the whole body) / no command extracted. Other
    static errors are surfaced in ``static_errors`` but, as in baseline, do
    not block extraction. No subprocess is ever started.
    """
    baseline = _load_baseline_module()

    def _static_error_row(error: Any) -> dict[str, Any]:
        return {
            "kind": error.kind,
            "line": error.line_number,
            "raw_line": error.raw_line,
            "fix_hint": error.fix_hint,
            "rule_id": error.rule_id,
        }

    def _blocked(errors: list[str], static_errors: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        return 2, {
            "status": "blocked",
            "commands": [],
            "command_hashes": [],
            "static_errors": static_errors,
            "errors": errors,
        }

    section = baseline.extract_verification_commands_section(body)
    if section is None:
        return _blocked(["VC001_NO_VERIFICATION_COMMANDS_SECTION"], [])
    command_tuples, parse_result = baseline._command_entries_from_shared_parser(section)
    static_errors = [_static_error_row(error) for error in parse_result.static_errors]
    non_dollar = [row for row in static_errors if row["kind"] == "non_dollar_command"]
    if non_dollar:
        return _blocked(["VC004_NON_DOLLAR_COMMAND"], static_errors)
    if not command_tuples:
        return _blocked(["VC002_NO_COMMANDS_EXTRACTED"], static_errors)

    commands = [
        {
            "ac": ac_label or "AC_UNKNOWN",
            "line": line_no,
            "raw_command": command,
            "command_hash": f"sha256:{baseline.compute_command_hash(command)}",
        }
        for (ac_label, command, line_no, *_rest) in command_tuples
    ]
    return 0, {
        "status": "ok",
        "commands": commands,
        "command_hashes": [row["command_hash"] for row in commands],
        "static_errors": static_errors,
        "errors": [],
    }
# --- end Issue #2837 production wiring ---------------------------------------


def _compact_payload(payload: dict[str, Any], *, truncated: bool, omitted_fields: list[str]) -> dict[str, Any]:
    compact = dict(payload)
    compact["stdout_truncated"] = truncated
    compact["omitted_fields"] = omitted_fields
    return compact


def _compact_output(payload: dict[str, Any], max_stdout_bytes: int) -> str:
    compact = _canonical_json(_compact_payload(payload, truncated=False, omitted_fields=[]))
    if len(compact.encode("utf-8")) <= max_stdout_bytes:
        return compact

    trimmed = _compact_payload(payload, truncated=True, omitted_fields=["per_ac.summary", "evidence_refs"])
    trimmed["per_ac"] = [
        {key: value for key, value in entry.items() if key != "summary"}
        for entry in payload["per_ac"]
    ]
    trimmed["evidence_refs"] = []
    compact = _canonical_json(trimmed)
    if len(compact.encode("utf-8")) <= max_stdout_bytes:
        return compact

    fail_closed = _compact_payload(
        _result(
            overall_status="indeterminate",
            per_ac=[],
            rerun_required=True,
            source_integrity=payload["source_integrity"],
            evidence_refs=[],
            artifact_ref=payload.get("artifact_ref"),
            artifact_digest=payload.get("artifact_digest"),
            errors=["stdout_budget_exceeded"],
            stdout_truncated=True,
            omitted_fields=["per_ac", "evidence_refs"],
        ),
        truncated=True,
        omitted_fields=["per_ac", "evidence_refs"],
    )
    return _canonical_json(fail_closed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adjudicate VC result against baseline")
    parser.add_argument(
        "command",
        nargs="?",
        default="adjudicate",
        choices=[
            "adjudicate",
            "step4-gate",
            "adapt",
            "step4-adjudicate",
            "step5-terminal-gate",
            "extract-vc-metadata",
        ],
        help=(
            "adjudicate (default): classify a current VC result against the "
            "baseline contract snapshot. step4-gate (Issue #88 fix_delta "
            "Blocker 1): re-derive the Step 4 invoke/rerun decision from a "
            "LOOP_STATE.vc_adjudication entry for an explicit current-head "
            "binding tuple, without re-running test-runner. adapt (Issue #88 "
            "fix_delta Blocker 1): convert a TEST_VERDICT_MACHINE/v2 report "
            "(--test-verdict-file) into the baseline_vc_preflight/v1 shape "
            "accepted by --current-vc-result-file. step4-adjudicate (Issue "
            "#2837): adapt -> adjudicate -> persist -> gate in one process, "
            "the canonical Step 4 entrance that records the reviewer "
            "dispatch (loop_state['dispatch']). step5-terminal-gate (Issue "
            "#2837): terminal approval composition point (route + dispatch "
            "seq/binding + VC gate). extract-vc-metadata (Issue #2837 "
            "fix_delta): parse-only extraction of the ordered (ac, line, "
            "raw_command, command_hash) of a live Issue body (--body-file) "
            "using baseline_vc_preflight's own helpers, without running any "
            "Verification Command."
        ),
    )
    parser.add_argument("--contract-snapshot-file")
    parser.add_argument(
        "--body-file",
        help="extract-vc-metadata: path to the live linked Issue body (read-only; nothing is executed).",
    )
    parser.add_argument("--current-vc-result-file")
    parser.add_argument("--diff-summary-file")
    parser.add_argument("--allowed-paths-file")
    parser.add_argument("--test-verdict-file")
    parser.add_argument(
        "--expected-issue-number",
        type=int,
        help=(
            "Issue #2467 P1: the live linked Issue number, independently retrieved "
            "by the caller, that a runtime_only current-head binding must exactly match."
        ),
    )
    parser.add_argument(
        "--expected-pr-number",
        type=int,
        help=(
            "Issue #2467 P1: the live current PR number, independently retrieved "
            "by the caller, that a runtime_only current-head binding must exactly match."
        ),
    )
    parser.add_argument(
        "--require-producer-receipt",
        action="store_true",
        help=(
            "Issue #1648: reject a pr_review_only test_verdict unless it embeds a "
            "schema-valid Child A producer_receipt bound to its own head_sha/pr_number/"
            "issue_number/contract_body_sha256/artifact digest. Legacy self-attested "
            "TEST_VERDICT input (no producer_receipt) stays accepted when this flag is "
            "absent."
        ),
    )
    parser.add_argument("--artifact-out")
    parser.add_argument("--max-stdout-bytes", type=int, default=4096)
    parser.add_argument(
        "--loop-state-file",
        help="step4-gate: path to a JSON LOOP_STATE snapshot containing 'vc_adjudication'.",
    )
    parser.add_argument("--expected-head-sha", help="step4-gate: current live PR head SHA.")
    parser.add_argument(
        "--expected-contract-body-sha256",
        help="step4-gate: current live linked Issue body SHA256.",
    )
    parser.add_argument(
        "--expected-command-hashes-file",
        help="step4-gate: path to a JSON array of literal Verification Command SHA256 hashes, in order.",
    )
    parser.add_argument(
        "--adapt-out",
        help="adapt: path to write the converted baseline_vc_preflight/v1 JSON (default: stdout).",
    )
    parser.add_argument(
        "--delegate-pr-review-only-nonpass",
        action="store_true",
        help=(
            "step4-adjudicate (Issue #2916, opt-in): on the independent pr_review_only "
            "route, record an EXECUTED non-pass item (FAIL / SKIP / fallback; the facts "
            "are kept verbatim in per_ac failure_keys, never rewritten to PASS) and allow "
            "a pr-reviewer DISPATCH. The AC stays blocking / unresolved and terminal "
            "approval still needs step5-terminal-gate. Without this flag a non-pass "
            "pr_review_only execution fails closed (pr_review_only_current_execution_not_pass)."
        ),
    )
    parser.add_argument(
        "--reuse-stored",
        action="store_true",
        help=(
            "step4-adjudicate: re-evaluate the stored PASS for the caller-supplied "
            "binding without reading a test-runner report (no adapt / adjudicate / "
            "invalidation); records the dispatch when the gate opens."
        ),
    )
    parser.add_argument(
        "--producer-exit-code",
        help=(
            "step4-adjudicate (Issue #2996): the ensure_contract_snapshot.py process exit code "
            "that produced --contract-snapshot-file. Required when that file is a "
            "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 envelope (status ok <=> 0, dry_run_would_post "
            "<=> 20; human_judgment is never accepted even though it also exits 20); rejected "
            "for any other snapshot shape and together with --reuse-stored. Parsed as a plain "
            "non-negative integer; anything else is a fail-closed input error."
        ),
    )
    parser.add_argument(
        "--repo",
        help=(
            "step4-adjudicate (Issue #2996): owner/repo used to re-verify the trusted "
            "contract_snapshot_url comment of an ok envelope (required for status ok, with "
            "--expected-issue-number; unused for dry_run_would_post). Rejected together with "
            "--reuse-stored."
        ),
    )
    parser.add_argument(
        "--reviewer-verdict-file",
        help="step5-terminal-gate: JSON reviewer_verdict (first input of route_loop_verdict_v2()).",
    )
    parser.add_argument(
        "--live-mergeability-file",
        help="step5-terminal-gate: JSON live_mergeability (second input of route_loop_verdict_v2()).",
    )
    parser.add_argument(
        "--dispatch-seq",
        type=int,
        help=(
            "step5-terminal-gate: the dispatch seq saved when the reviewer was "
            "dispatched (never re-read from loop_state after resume)."
        ),
    )
    args = parser.parse_args(argv)
    if args.command != "step4-adjudicate" and (
        args.producer_exit_code is not None or args.repo is not None
    ):
        parser.error("--producer-exit-code and --repo are only accepted by step4-adjudicate")
    if args.command == "adjudicate":
        if not args.contract_snapshot_file or not args.current_vc_result_file:
            parser.error(
                "adjudicate requires --contract-snapshot-file and --current-vc-result-file"
            )
    elif args.command == "adapt":
        if not args.test_verdict_file:
            parser.error("adapt requires --test-verdict-file")
    elif args.command == "extract-vc-metadata":
        if not args.body_file:
            parser.error("extract-vc-metadata requires --body-file")
    elif args.command == "step4-adjudicate":
        required = [
            ("--loop-state-file", args.loop_state_file),
            ("--expected-head-sha", args.expected_head_sha),
            ("--expected-contract-body-sha256", args.expected_contract_body_sha256),
            ("--expected-command-hashes-file", args.expected_command_hashes_file),
        ]
        if not args.reuse_stored:
            required += [
                ("--test-verdict-file", args.test_verdict_file),
                ("--contract-snapshot-file", args.contract_snapshot_file),
                ("--diff-summary-file", args.diff_summary_file),
                ("--allowed-paths-file", args.allowed_paths_file),
            ]
        missing = [flag for flag, value in required if not value]
        if missing:
            parser.error(f"step4-adjudicate requires: {', '.join(missing)}")
        if args.reuse_stored and (args.producer_exit_code is not None or args.repo is not None):
            # --reuse-stored never re-reads a snapshot, so a handoff argument
            # alongside it would silently claim a verification that did not run.
            parser.error("step4-adjudicate --reuse-stored cannot be combined with --producer-exit-code or --repo")
    elif args.command == "step5-terminal-gate":
        missing = [
            flag
            for flag, value in (
                ("--loop-state-file", args.loop_state_file),
                ("--reviewer-verdict-file", args.reviewer_verdict_file),
                ("--live-mergeability-file", args.live_mergeability_file),
                ("--expected-head-sha", args.expected_head_sha),
                ("--expected-contract-body-sha256", args.expected_contract_body_sha256),
                ("--expected-command-hashes-file", args.expected_command_hashes_file),
            )
            if not value
        ]
        if args.dispatch_seq is None:
            missing.append("--dispatch-seq")
        if missing:
            parser.error(f"step5-terminal-gate requires: {', '.join(missing)}")
    else:
        missing = [
            flag
            for flag, value in (
                ("--loop-state-file", args.loop_state_file),
                ("--expected-head-sha", args.expected_head_sha),
                ("--expected-contract-body-sha256", args.expected_contract_body_sha256),
                ("--expected-command-hashes-file", args.expected_command_hashes_file),
            )
            if not value
        ]
        if missing:
            parser.error(f"step4-gate requires: {', '.join(missing)}")
    return args


def _load_allowed_paths(path: str | None) -> tuple[list[str] | None, list[str]]:
    if path is None:
        return None, ["missing_allowed_paths_file"]
    raw, errors = _load_json_file(path)
    if errors:
        return None, errors
    if not isinstance(raw, list):
        return None, ["allowed_paths_not_list"]
    return list(raw), []


def _run_adapt(args: argparse.Namespace) -> int:
    """CLI entrypoint for the `adapt` subcommand (Issue #88 fix_delta
    Blocker 1). Exit codes: 0 = converted cleanly, 1 = converted with
    warnings (see stderr), 2 = --test-verdict-file unreadable/not JSON or
    not interpretable as TEST_VERDICT_MACHINE/v2 at all."""
    raw, load_errors = _load_json_file(args.test_verdict_file)
    if load_errors:
        sys.stdout.write(_canonical_json({"errors": load_errors}) + "\n")
        return 2

    converted, errors = adapt_test_verdict_to_current_vc_result(raw)
    if converted is None:
        sys.stdout.write(_canonical_json({"errors": errors}) + "\n")
        return 2

    text_out = _canonical_json(converted)
    if args.adapt_out:
        Path(args.adapt_out).write_text(text_out, encoding="utf-8")
    else:
        sys.stdout.write(text_out + "\n")

    if errors:
        sys.stderr.write(_canonical_json({"warnings": errors}) + "\n")
        return 1
    return 0


def _run_step4_gate(args: argparse.Namespace) -> int:
    """CLI entrypoint for the `step4-gate` subcommand (Issue #88 fix_delta
    Blocker 1). Exit codes: 0 = invoke pr-reviewer, 1 = rerun (Step 2 must
    run test-runner again), 2 = malformed CLI input (unparseable
    --loop-state-file / --expected-command-hashes-file, or a
    non-object LOOP_STATE payload)."""
    loop_state, loop_state_errors = _load_json_file(args.loop_state_file)
    if loop_state_errors or not isinstance(loop_state, dict):
        sys.stdout.write(
            _canonical_json(
                {
                    "invoke_pr_reviewer": False,
                    "reason_code": "loop_state_malformed",
                    "reused": False,
                    "errors": loop_state_errors or ["loop_state_not_object"],
                }
            )
            + "\n"
        )
        return 2

    expected_command_hashes, hashes_errors = _load_json_file(args.expected_command_hashes_file)
    if hashes_errors or not isinstance(expected_command_hashes, list):
        sys.stdout.write(
            _canonical_json(
                {
                    "invoke_pr_reviewer": False,
                    "reason_code": "expected_command_hashes_malformed",
                    "reused": False,
                    "errors": hashes_errors or ["expected_command_hashes_not_list"],
                }
            )
            + "\n"
        )
        return 2

    decision = step4_gate_from_loop_state(
        loop_state,
        expected_head_sha=args.expected_head_sha,
        expected_contract_body_sha256=args.expected_contract_body_sha256,
        expected_command_hashes=list(expected_command_hashes),
    )
    sys.stdout.write(_canonical_json(decision) + "\n")
    return 0 if decision["invoke_pr_reviewer"] else 1


def _run_extract_vc_metadata(args: argparse.Namespace) -> int:
    """CLI entrypoint for `extract-vc-metadata` (Issue #2837 fix_delta).

    Read-only and parse-only: reads ``--body-file`` and prints
    ``extract_vc_metadata()``'s JSON. Exit codes: 0 = extracted, 2 = body
    unreadable or an extraction failure baseline_vc_preflight.py also rejects
    with exit 2 (no section / ``non_dollar_command`` / no command)."""
    try:
        body = Path(args.body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        sys.stdout.write(
            _canonical_json(
                {
                    "status": "blocked",
                    "commands": [],
                    "command_hashes": [],
                    "static_errors": [],
                    "errors": [f"body_file_unreadable:{type(exc).__name__}"],
                }
            )
            + "\n"
        )
        return 2
    exit_code, payload = extract_vc_metadata(body)
    sys.stdout.write(_canonical_json(payload) + "\n")
    return exit_code


def _write_json_atomically(path: str, value: Any) -> None:
    """Write ``value`` as JSON via a same-directory temp file + rename."""
    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json(value))
            handle.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _emit_malformed(reason_code: str, errors: list[str]) -> int:
    sys.stdout.write(
        _canonical_json({"invoke_pr_reviewer": False, "reason_code": reason_code, "errors": errors}) + "\n"
    )
    return 2


REASON_STATE_PERSIST_FAILED = "state_persist_failed_invalidation_unconfirmed"


def _best_effort_invalidate_stored_pass(path: str, binding_key: str) -> bool:
    """After a failed LOOP_STATE write, try once to drop the stored PASS for
    ``binding_key`` from the on-disk state. Returns True only when the
    on-disk state is known not to hold that entry. No WAL / lock / ledger."""
    try:
        if not Path(path).exists():
            return True
        state, _errors = _load_loop_state_for_update(path, create_if_absent=False)
        if state is None:
            return False
        entries = state.get("vc_adjudication")
        if isinstance(entries, dict) and binding_key in entries:
            entries.pop(binding_key)
            _write_json_atomically(path, state)
        return True
    except Exception:
        return False


def _persist_loop_state_or_report(
    args: argparse.Namespace, loop_state: dict[str, Any], *, invalidate_on_failure: bool
) -> int | None:
    """Persist ``loop_state``. Returns ``None`` on success. On failure prints a
    structured output distinct from an ordinary rerun (exit 1) and returns 2
    (the same exit code as malformed input; ``reason_code`` discriminates):
    the in-memory invalidation / new dispatch is NOT persisted, so a PASS
    stored before this invocation may still be on disk and must not be reused
    via ``--reuse-stored`` until adjudicate + persist is redone."""
    try:
        _write_json_atomically(args.loop_state_file, loop_state)
    except Exception as exc:
        binding_key = step4_binding_key(
            head_sha=args.expected_head_sha,
            contract_body_sha256=args.expected_contract_body_sha256,
            command_hashes=_load_json_file(args.expected_command_hashes_file)[0] or [],
        )
        invalidated = (
            _best_effort_invalidate_stored_pass(args.loop_state_file, binding_key)
            if invalidate_on_failure
            else False
        )
        sys.stdout.write(
            _canonical_json(
                {
                    "invoke_pr_reviewer": False,
                    "reason_code": REASON_STATE_PERSIST_FAILED,
                    "binding_key": binding_key,
                    "seq": None,
                    "stale_pass_invalidated_best_effort": invalidated,
                    "errors": [f"state_persist_failed:{type(exc).__name__}"],
                }
            )
            + "\n"
        )
        return 2
    return None


def _load_loop_state_for_update(path: str, *, create_if_absent: bool) -> tuple[dict[str, Any] | None, list[str]]:
    """Load LOOP_STATE for read-modify-write. An absent file is an empty
    mapping when ``create_if_absent``; an unreadable / non-object file is an
    error (the caller must not write anything)."""
    if create_if_absent and not Path(path).exists():
        return {}, []
    loaded, errors = _load_json_file(path)
    if errors:
        return None, errors
    if not isinstance(loaded, dict):
        return None, ["loop_state_not_object"]
    return loaded, []


def _run_step4_adjudicate(args: argparse.Namespace) -> int:
    """CLI entrypoint for `step4-adjudicate` (Issue #2837). Exit codes:
    0 = invoke (dispatch permitted and recorded), 1 = rerun, 2 = malformed
    (corrupt LOOP_STATE / expected-command-hashes; nothing is written) or a
    LOOP_STATE persist failure (``reason_code`` is
    ``state_persist_failed_invalidation_unconfirmed``: invalidation / dispatch
    could not be saved, so it is not an ordinary "invalidated" rerun)."""
    loop_state, state_errors = _load_loop_state_for_update(args.loop_state_file, create_if_absent=True)
    if loop_state is None:
        return _emit_malformed("loop_state_malformed", state_errors)
    if "dispatch" in loop_state and _dispatch_record(loop_state) is None:
        return _emit_malformed("loop_state_dispatch_malformed", ["dispatch_missing_or_malformed"])
    if "vc_adjudication" in loop_state and not isinstance(loop_state["vc_adjudication"], dict):
        return _emit_malformed("loop_state_malformed", ["vc_adjudication_not_object"])

    expected_hashes, hashes_errors = _load_json_file(args.expected_command_hashes_file)
    if hashes_errors or not isinstance(expected_hashes, list) or not all(
        isinstance(item, str) for item in expected_hashes
    ):
        return _emit_malformed(
            "expected_command_hashes_malformed", hashes_errors or ["expected_command_hashes_not_string_list"]
        )

    if args.reuse_stored:
        exit_code, payload = step4_adjudicate(
            loop_state,
            expected_head_sha=args.expected_head_sha,
            expected_contract_body_sha256=args.expected_contract_body_sha256,
            expected_command_hashes=expected_hashes,
            reuse_stored=True,
        )
        if exit_code == 0:
            failed = _persist_loop_state_or_report(args, loop_state, invalidate_on_failure=False)
            if failed is not None:
                return failed
        sys.stdout.write(_canonical_json(payload) + "\n")
        return exit_code

    test_verdict, test_verdict_errors = _load_json_file(args.test_verdict_file)
    contract_snapshot, contract_errors = _load_json_file(args.contract_snapshot_file)
    # Issue #2996: the envelope handoff lives INSIDE this input pre-processing.
    # A failure yields contract_snapshot=None plus structured errors, so the
    # adjudicate -> persist path below still runs and invalidates a stale PASS
    # stored for this binding (never an early return before persist).
    try:
        contract_snapshot, resolve_errors = resolve_step4_contract_snapshot(
            contract_snapshot,
            producer_exit_code=args.producer_exit_code,
            repo=args.repo,
            expected_issue_number=args.expected_issue_number,
            expected_contract_body_sha256=args.expected_contract_body_sha256,
        )
    except Exception as exc:  # defense in depth: fail closed, never skip persist
        contract_snapshot = None
        resolve_errors = [f"contract_snapshot_resolution_error:{type(exc).__name__}"]
    contract_errors = list(contract_errors or []) + resolve_errors
    diff_summary, diff_errors = _load_json_file(args.diff_summary_file)
    allowed_paths, allowed_errors = _load_allowed_paths(args.allowed_paths_file)

    exit_code, payload = step4_adjudicate(
        loop_state,
        expected_head_sha=args.expected_head_sha,
        expected_contract_body_sha256=args.expected_contract_body_sha256,
        expected_command_hashes=expected_hashes,
        test_verdict=test_verdict,
        test_verdict_errors=test_verdict_errors,
        contract_snapshot=contract_snapshot,
        contract_snapshot_errors=contract_errors,
        diff_summary=diff_summary,
        diff_summary_errors=diff_errors,
        allowed_paths=allowed_paths,
        allowed_paths_errors=allowed_errors,
        expected_issue_number=args.expected_issue_number,
        expected_pr_number=args.expected_pr_number,
        require_producer_receipt=args.require_producer_receipt,
        delegate_pr_review_only_nonpass=args.delegate_pr_review_only_nonpass,
    )
    failed = _persist_loop_state_or_report(args, loop_state, invalidate_on_failure=True)
    if failed is not None:
        return failed
    sys.stdout.write(_canonical_json(payload) + "\n")
    return exit_code


def _run_step5_terminal_gate(args: argparse.Namespace) -> int:
    """CLI entrypoint for `step5-terminal-gate` (Issue #2837). Exit codes:
    0 = approved, 1 = not approved (route JSON on stdout), 2 = malformed
    (unreadable arguments or corrupt LOOP_STATE). Read-only."""
    loop_state, state_errors = _load_loop_state_for_update(args.loop_state_file, create_if_absent=False)
    if loop_state is None:
        return _emit_malformed("loop_state_malformed", state_errors)
    reviewer_verdict, verdict_errors = _load_json_file(args.reviewer_verdict_file)
    if verdict_errors:
        return _emit_malformed("reviewer_verdict_malformed", verdict_errors)
    live_mergeability, live_errors = _load_json_file(args.live_mergeability_file)
    if live_errors:
        return _emit_malformed("live_mergeability_malformed", live_errors)
    expected_hashes, hashes_errors = _load_json_file(args.expected_command_hashes_file)
    if hashes_errors or not isinstance(expected_hashes, list) or not all(
        isinstance(item, str) for item in expected_hashes
    ):
        return _emit_malformed(
            "expected_command_hashes_malformed", hashes_errors or ["expected_command_hashes_not_string_list"]
        )

    exit_code, decision = step5_terminal_gate(
        loop_state,
        reviewer_verdict,
        live_mergeability,
        expected_head_sha=args.expected_head_sha,
        expected_contract_body_sha256=args.expected_contract_body_sha256,
        expected_command_hashes=expected_hashes,
        dispatch_seq=args.dispatch_seq,
    )
    sys.stdout.write(_canonical_json(decision) + "\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command == "step4-gate":
        return _run_step4_gate(args)
    if args.command == "adapt":
        return _run_adapt(args)
    if args.command == "step4-adjudicate":
        return _run_step4_adjudicate(args)
    if args.command == "step5-terminal-gate":
        return _run_step5_terminal_gate(args)
    if args.command == "extract-vc-metadata":
        return _run_extract_vc_metadata(args)

    contract_snapshot, contract_errors = _load_json_file(args.contract_snapshot_file)
    current_vc_result, current_errors = _load_json_file(args.current_vc_result_file)
    diff_summary, diff_errors = _load_json_file(args.diff_summary_file) if args.diff_summary_file else (None, [])
    diff_errors = diff_errors or []
    allowed_paths, allowed_errors = _load_allowed_paths(args.allowed_paths_file)
    test_verdict, test_verdict_errors = (
        _load_json_file(args.test_verdict_file) if args.test_verdict_file else (None, [])
    )

    result = adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=test_verdict,
        require_producer_receipt=args.require_producer_receipt,
        expected_issue_number=args.expected_issue_number,
        expected_pr_number=args.expected_pr_number,
    )
    result["errors"].extend(contract_errors or [])
    result["errors"].extend(current_errors or [])
    result["errors"].extend(diff_errors or [])
    result["errors"].extend(allowed_errors or [])
    result["errors"].extend(test_verdict_errors or [])
    if result["errors"]:
        result["overall_status"] = "indeterminate"
        result["blocking"] = True
        result["rerun_required"] = True

    if args.artifact_out:
        bundle = {
            "schema": PRIVATE_BUNDLE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "contract_snapshot": contract_snapshot,
            "current_vc_result": current_vc_result,
            "diff_summary": diff_summary,
            "allowed_paths": allowed_paths,
            "test_verdict": test_verdict,
            "artifact_inputs": {
                "contract_snapshot_file": args.contract_snapshot_file,
                "current_vc_result_file": args.current_vc_result_file,
                "diff_summary_file": args.diff_summary_file,
                "allowed_paths_file": args.allowed_paths_file,
                "test_verdict_file": args.test_verdict_file,
            },
            "result": result,
        }
        bundle_text = _canonical_json(bundle)
        Path(args.artifact_out).write_text(bundle_text, encoding="utf-8")
        result["artifact_ref"] = PRIVATE_ARTIFACT_REF
        result["artifact_digest"] = _sha256(bundle_text)

    sys.stdout.write(_compact_output(result, args.max_stdout_bytes) + "\n")
    return 0 if not result["blocking"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
