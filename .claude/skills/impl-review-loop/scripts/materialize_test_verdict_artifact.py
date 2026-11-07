#!/usr/bin/env python3
"""materialize_test_verdict_artifact.py

Issue #1648 (Child C of #1645): receipt-aware materializer that turns a
Child B (Issue #1647) TEST_VERDICT_PUBLISH_INPUT_V1-shaped request into a
TEST_VERDICT_MACHINE/v2 input bundle (consumable by
adjudicate_vc_result.py --test-verdict-file --require-producer-receipt)
and a private/audit bundle.

Issue #1648 OWNER fix_delta (PR #1831 review): this module now performs a
*live* GitHub readback itself instead of trusting a caller-supplied
``execution_record``. It imports and re-executes Child B's exact live
readback functions (``_verify_producer_run_and_job`` /
``_verify_execution_artifact_metadata`` / ``_download_and_verify_artifact_archive``,
all defined in ``scripts/agent-guards/controlled_skill_mutation_exec.py``)
so that materialization can never proceed from a purely self-attested,
internally-consistent-but-fabricated ``producer_receipt``. The execution
record used for binding is *only* the one actually downloaded and digest
-verified from the GitHub Actions artifact the receipt claims -- callers
can no longer substitute an arbitrary in-memory execution record.

Fail-closed contract (Issue #1648 AC1/AC6): materialization only produces an
input/private bundle when ALL of the following bindings hold. Any mismatch
returns status "blocked" with zero output files written (and any stale
existing output files at the target paths are invalidated -- Issue #1648
fix_delta AC10):

  - the Child B publish_request (schema TEST_VERDICT_PUBLISH_INPUT_V1)
    passes the exact same field/schema validation Child B's dedicated
    publisher enforces (reused directly from
    scripts/agent-guards/controlled_skill_mutation_exec.py
    ``_validate_test_verdict_publish_fields``, which itself fully validates
    the embedded ``producer_receipt`` against
    schemas/test-verdict-producer-receipt.schema.json)
  - the publish_request's target_pr_number / expected_head_sha /
    linked_issue_number / linked_issue_body_sha256 match the *current*
    (independently supplied) live GitHub state -- this is the freshness /
    drift guard that a merely internally-consistent-but-stale publish
    request cannot satisfy
  - the receipt's claimed workflow run / job / check run actually exist on
    GitHub and match the expected head SHA and repository (live readback,
    Issue #1648 fix_delta AC6)
  - the receipt's claimed artifact actually exists, is not expired, and its
    archive bytes actually download and hash to the receipt's declared
    ``artifact_archive_digest`` (live readback + download, Issue #1648
    fix_delta AC6)
  - the downloaded execution record hashes to the receipt's declared
    execution_payload_sha256, and its subject/contract exactly match the
    receipt's subject/contract
  - every AC referenced by the downloaded execution_record's per_ac
    coverage resolves to a caller-supplied command_hash (sourced from the
    Issue contract snapshot's vc_preflight classifications), to real
    executions, and each of those executions independently satisfies
    exit_code == 0, status == "pass", skipped in (False, None), timed_out
    in (False, None), and fallback_detected == False (Issue #1648 fix_delta
    AC7 -- no blanket status/fallback_detected-only shortcut, and no
    unconditional exit_code normalization on output)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "MATERIALIZE_TEST_VERDICT_RESULT_V1"
SCHEMA_VERSION = 1
INPUT_BUNDLE_SCHEMA = "TEST_VERDICT_MACHINE/v2"
PRIVATE_BUNDLE_SCHEMA = "TEST_VERDICT_MATERIALIZE_PRIVATE_BUNDLE_V1"
PUBLISH_REQUEST_SCHEMA = "TEST_VERDICT_PUBLISH_INPUT_V1"
EXECUTION_RECORD_SCHEMA = "TEST_VERDICT_EXECUTION_RECORD_V1"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONTROLLED_EXEC_PATH = _REPO_ROOT / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"

_CONTROLLED_EXEC_MODULE = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _load_json_file(path: str | None) -> tuple[Any, list[str]]:
    if not path:
        return None, ["missing_input_file"]
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [f"input_file_not_found:{path}"]
    except OSError as exc:
        return None, [f"input_read_error:{path}:{type(exc).__name__}"]
    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        return None, [f"input_json_error:{path}:{exc}"]


def _load_controlled_exec_module():
    """Lazily import scripts/agent-guards/controlled_skill_mutation_exec.py
    (DRY, single source of truth) so the materializer can reuse both Child
    B's exact TEST_VERDICT_PUBLISH_INPUT_V1 field validator *and* Child B's
    exact live GitHub readback / artifact download functions (Issue #1648
    fix_delta AC6)."""
    global _CONTROLLED_EXEC_MODULE
    if _CONTROLLED_EXEC_MODULE is not None:
        return _CONTROLLED_EXEC_MODULE, None
    spec = importlib.util.spec_from_file_location(
        "controlled_skill_mutation_exec_for_materializer", _CONTROLLED_EXEC_PATH
    )
    if spec is None or spec.loader is None:
        return None, "controlled_skill_mutation_exec_module_unavailable"
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover
        return None, f"controlled_skill_mutation_exec_import_failed:{type(exc).__name__}"
    _CONTROLLED_EXEC_MODULE = module
    return _CONTROLLED_EXEC_MODULE, None


def _load_publish_validator():
    module, import_error = _load_controlled_exec_module()
    if module is None:
        return None, import_error
    validator = getattr(module, "_validate_test_verdict_publish_fields", None)
    if validator is None:
        return None, "controlled_skill_mutation_exec_missing_validator"
    return validator, None


def _live_readback_execution_record(
    receipt: dict[str, Any],
    repo: str,
    expected_head_sha: str,
    gh_bin: str | None,
    gh_env: dict[str, str] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Issue #1648 fix_delta AC6: perform the actual live GitHub readback
    (workflow run / job / check run identity, artifact metadata, artifact
    archive download + digest recomputation) that Child B's dedicated
    publisher performs before ever accepting a receipt as evidence. Returns
    (record, errors) -- record is the parsed execution-record payload
    actually inside the downloaded artifact archive, never a caller-
    supplied value."""
    module, import_error = _load_controlled_exec_module()
    if module is None:
        return None, [import_error]

    resolved_gh_bin = gh_bin
    if resolved_gh_bin is None:
        find_gh_bin = getattr(module, "_find_gh_bin", None)
        if find_gh_bin is None:
            return None, ["controlled_skill_mutation_exec_missing_find_gh_bin"]
        resolved_gh_bin, gh_bin_error = find_gh_bin()
        if resolved_gh_bin is None:
            return None, [gh_bin_error or "gh_not_found_in_trusted_path"]

    resolved_env = gh_env
    if resolved_env is None:
        build_env = getattr(module, "_build_pr_review_gh_env", None)
        if build_env is None:
            return None, ["controlled_skill_mutation_exec_missing_gh_env_builder"]
        resolved_env = build_env()

    verify_producer_run_and_job = getattr(module, "_verify_producer_run_and_job", None)
    verify_execution_artifact_metadata = getattr(module, "_verify_execution_artifact_metadata", None)
    download_and_verify_artifact_archive = getattr(module, "_download_and_verify_artifact_archive", None)
    if (
        verify_producer_run_and_job is None
        or verify_execution_artifact_metadata is None
        or download_and_verify_artifact_archive is None
    ):
        return None, ["controlled_skill_mutation_exec_missing_live_readback_functions"]

    live_err = verify_producer_run_and_job(receipt, repo, expected_head_sha, resolved_gh_bin, resolved_env)
    if live_err:
        return None, [f"test_verdict_materialize_live_readback_failed:{live_err}"]

    meta_err = verify_execution_artifact_metadata(receipt, repo, resolved_gh_bin, resolved_env)
    if meta_err:
        return None, [f"test_verdict_materialize_live_readback_failed:{meta_err}"]

    record, archive_err = download_and_verify_artifact_archive(receipt, repo, resolved_gh_bin, resolved_env)
    if archive_err:
        return None, [f"test_verdict_materialize_live_readback_failed:{archive_err}"]
    if not isinstance(record, dict):
        return None, ["test_verdict_materialize_live_readback_record_invalid"]
    return record, []


def _validate_execution_record(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["execution_record_not_object"]
    errors: list[str] = []
    if record.get("schema") != EXECUTION_RECORD_SCHEMA:
        errors.append("execution_record_schema_mismatch")
    payload_sha256 = record.get("payload_sha256")
    if not isinstance(payload_sha256, str) or not payload_sha256:
        errors.append("execution_record_payload_sha256_missing")
    else:
        stripped = {key: value for key, value in record.items() if key != "payload_sha256"}
        if _canonical_sha256(stripped) != payload_sha256:
            errors.append("execution_record_payload_sha256_mismatch")
    if record.get("pass_eligible") is not True:
        errors.append("execution_record_not_pass_eligible")
    if not isinstance(record.get("executions"), list) or not record.get("executions"):
        errors.append("execution_record_executions_missing")
    if not isinstance(record.get("per_ac"), list) or not record.get("per_ac"):
        errors.append("execution_record_per_ac_missing")
    if not isinstance(record.get("subject"), dict):
        errors.append("execution_record_subject_missing")
    if not isinstance(record.get("contract"), dict):
        errors.append("execution_record_contract_missing")
    return errors


def _execution_satisfies_pass(execution: dict[str, Any]) -> bool:
    """Issue #1648 fix_delta AC7: strict per-execution PASS gate. All of
    exit_code == 0, status == "pass", skipped in (False, None), timed_out in
    (False, None), and fallback_detected == False must hold individually --
    a mere status == "pass" (with an unchecked non-zero exit_code, or a
    skipped/timed_out execution) is never treated as PASS."""
    return (
        execution.get("exit_code") == 0
        and execution.get("status") == "pass"
        and execution.get("skipped") in (False, None)
        and execution.get("timed_out") in (False, None)
        and execution.get("fallback_detected") is False
    )


def _build_runtime_ac_results(
    record: dict[str, Any], command_hash_map: dict[str, str]
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    executions_by_id: dict[str, dict[str, Any]] = {}
    for execution in record.get("executions", []):
        if not isinstance(execution, dict) or not isinstance(execution.get("execution_id"), str):
            errors.append("execution_record_execution_entry_invalid")
            continue
        execution_id = execution["execution_id"]
        if execution_id in executions_by_id:
            errors.append(f"execution_record_execution_id_duplicate:{execution_id}")
            continue
        executions_by_id[execution_id] = execution

    runtime_ac_results: list[dict[str, Any]] = []
    seen_acs: set[str] = set()
    referenced_execution_ids: set[str] = set()
    for entry in record.get("per_ac", []):
        if not isinstance(entry, dict):
            errors.append("execution_record_per_ac_entry_invalid")
            continue
        ac = entry.get("ac")
        execution_ids = entry.get("execution_ids")
        if not isinstance(ac, str) or not ac or not isinstance(execution_ids, list) or not execution_ids:
            errors.append("execution_record_per_ac_entry_invalid")
            continue
        if ac in seen_acs:
            errors.append(f"execution_record_per_ac_duplicate:{ac}")
            continue
        seen_acs.add(ac)

        command_hash = command_hash_map.get(ac)
        if not isinstance(command_hash, str) or not command_hash:
            errors.append(f"command_hash_map_missing_ac:{ac}")
            continue

        ac_pass = True
        exit_codes_seen: list[Any] = []
        fallback_detected = False
        for execution_id in execution_ids:
            referenced_execution_ids.add(execution_id)
            execution = executions_by_id.get(execution_id)
            if execution is None:
                errors.append(f"execution_record_execution_id_unresolved:{execution_id}")
                ac_pass = False
                continue
            exit_codes_seen.append(execution.get("exit_code"))
            if execution.get("fallback_detected"):
                fallback_detected = True
            if not _execution_satisfies_pass(execution):
                ac_pass = False

        status = "pass" if ac_pass and execution_ids else "fail"
        # Issue #1648 fix_delta AC7: reflect the actual input exit_code
        # rather than unconditionally normalizing to 0/1 on output.
        exit_code = exit_codes_seen[0] if exit_codes_seen else 1
        runtime_ac_results.append(
            {
                "ac": ac,
                "command_hash": command_hash,
                "exit_code": exit_code,
                "status": status,
                "fallback_detected": fallback_detected,
                "human_review_required": False,
                "stop_condition_triggered": False,
            }
        )

    expected_acs = set(command_hash_map.keys())
    missing_acs = expected_acs - seen_acs
    if missing_acs:
        errors.append(f"execution_record_per_ac_missing_acs:{sorted(missing_acs)}")

    unreferenced_execution_ids = set(executions_by_id) - referenced_execution_ids
    if unreferenced_execution_ids:
        errors.append(f"execution_record_unreferenced_executions:{sorted(unreferenced_execution_ids)}")

    return runtime_ac_results, errors


def materialize_test_verdict_artifact(
    *,
    publish_request: Any,
    command_hash_map: Any,
    repo: str,
    issue_number: int,
    current_pr_number: int,
    current_head_sha: str,
    current_issue_body_sha256: str,
    reviewed_head_sha: str,
    diff_head_sha: str,
    run_id: str,
    run_url: str,
    gh_bin: str | None = None,
    gh_env: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Return (status, input_bundle, private_bundle, errors).

    status is "ok" (bundles materialized) or "blocked" (fail-closed, no
    bundle files should be written by the caller). Issue #1648 fix_delta:
    the execution record is no longer caller-supplied -- it is obtained via
    a live GitHub readback of the publish_request's embedded
    producer_receipt, which is why this function no longer accepts an
    ``execution_record`` argument."""
    errors: list[str] = []

    if not isinstance(publish_request, dict):
        errors.append("publish_request_not_object")
    elif publish_request.get("schema") != PUBLISH_REQUEST_SCHEMA:
        errors.append("publish_request_schema_mismatch")

    if not isinstance(command_hash_map, dict) or not command_hash_map:
        errors.append("command_hash_map_invalid")
        command_hash_map = {}

    if errors:
        return "blocked", None, None, errors

    validator, import_error = _load_publish_validator()
    if validator is None:
        return "blocked", None, None, [import_error]

    publish_error = validator(publish_request, repo, issue_number)
    if publish_error:
        return "blocked", None, None, [publish_error]

    receipt = publish_request["producer_receipt"]

    # Freshness / drift guard: the publish_request proved internally
    # consistent above, but that alone does not prove it reflects the
    # *current* live GitHub state -- bind it against independently supplied
    # current values (Issue #1648 AC1 "current Issue/PR/HEAD/body SHA")
    # before spending any network calls on a stale request.
    if publish_request.get("target_pr_number") != current_pr_number:
        errors.append("current_pr_number_mismatch")
    if publish_request.get("expected_head_sha") != current_head_sha:
        errors.append("current_head_sha_mismatch")
    if publish_request.get("linked_issue_body_sha256") != current_issue_body_sha256:
        errors.append("current_issue_body_sha256_mismatch")
    if publish_request.get("linked_issue_number") != issue_number:
        errors.append("current_issue_number_mismatch")

    if errors:
        return "blocked", None, None, errors

    # Issue #1648 fix_delta AC6: live readback of the receipt's claimed
    # workflow run/job/check run and artifact, plus an actual archive
    # download + digest recomputation. The execution_record used below is
    # only ever the record parsed out of that downloaded archive.
    execution_record, live_errors = _live_readback_execution_record(
        receipt, repo, current_head_sha, gh_bin, gh_env
    )
    if live_errors:
        return "blocked", None, None, live_errors

    errors.extend(_validate_execution_record(execution_record))
    if errors:
        return "blocked", None, None, errors

    # Artifact digest binding: the downloaded execution_record must be the
    # exact one the receipt endorses (also cross-checked inside the live
    # readback itself; repeated here for defense in depth).
    if receipt.get("execution_payload_sha256") != execution_record.get("payload_sha256"):
        errors.append("execution_record_receipt_digest_mismatch")
    if receipt.get("subject") != execution_record.get("subject"):
        errors.append("execution_record_receipt_subject_mismatch")
    if receipt.get("contract") != execution_record.get("contract"):
        errors.append("execution_record_receipt_contract_mismatch")

    if errors:
        return "blocked", None, None, errors

    runtime_ac_results, ac_errors = _build_runtime_ac_results(execution_record, command_hash_map)
    if ac_errors:
        return "blocked", None, None, ac_errors

    command_hashes = sorted(command_hash_map.values())
    artifact_payload = {
        "issue_number": issue_number,
        "pr_number": current_pr_number,
        "head_sha": current_head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "diff_head_sha": diff_head_sha,
        "contract_body_sha256": current_issue_body_sha256,
        "command_hashes": command_hashes,
    }
    producer = receipt.get("producer") or {}
    execution_artifact = receipt.get("execution_artifact") or {}
    overall_pass = bool(runtime_ac_results) and all(item["status"] == "pass" for item in runtime_ac_results)

    input_bundle: dict[str, Any] = {
        "schema": INPUT_BUNDLE_SCHEMA,
        "producer_kind": "test-runner",
        "repository": repo,
        "issue_number": issue_number,
        "pr_number": current_pr_number,
        "head_sha": current_head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "diff_head_sha": diff_head_sha,
        "contract_body_sha256": current_issue_body_sha256,
        "run_id": run_id,
        "run_url": run_url,
        "workflow_run_id": producer.get("workflow_run_id"),
        "workflow_run_attempt": producer.get("workflow_run_attempt"),
        "check_run_id": producer.get("check_run_id"),
        "artifact": {
            "name": "test-verdict-machine",
            "artifact_digest": execution_artifact.get("artifact_archive_digest"),
            "url": execution_artifact.get("artifact_url"),
        },
        "artifact_payload": artifact_payload,
        "artifact_payload_sha256": _canonical_sha256(artifact_payload),
        "result": "PASS" if overall_pass else "FAIL",
        "verification_commands_pass": sum(1 for item in runtime_ac_results if item["status"] == "pass"),
        "verification_commands_fail": sum(1 for item in runtime_ac_results if item["status"] != "pass"),
        "verification_skipped_count": 0,
        "runtime_ac_results": runtime_ac_results,
        "producer_receipt": receipt,
        "receipt_sha256": publish_request.get("receipt_sha256"),
    }

    private_bundle: dict[str, Any] = {
        "schema": PRIVATE_BUNDLE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "issue_number": issue_number,
        "pr_number": current_pr_number,
        "head_sha": current_head_sha,
        "execution_record": execution_record,
        "publish_request": publish_request,
        "verification_trace": [
            "live_producer_run_and_job_verified",
            "live_execution_artifact_metadata_verified",
            "artifact_archive_downloaded_and_digest_verified",
            "execution_record_digest_verified",
            "publish_request_field_and_receipt_schema_verified",
            "current_state_binding_verified",
            "runtime_ac_results_materialized",
        ],
        "input_bundle_sha256": _canonical_sha256(input_bundle),
    }

    return "ok", input_bundle, private_bundle, []


def _build_result(
    status: str, input_bundle: dict[str, Any] | None, private_bundle: dict[str, Any] | None, errors: list[str]
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "input_bundle_present": input_bundle is not None,
        "private_bundle_present": private_bundle is not None,
        "input_bundle_sha256": _canonical_sha256(input_bundle) if input_bundle is not None else None,
        "errors": errors,
    }


def _atomic_write(path_str: str, content: str) -> None:
    """Issue #1648 fix_delta AC10: publish output bundles atomically (write
    to a temp file in the same directory, then os.replace()) so a crash or
    concurrent read can never observe a partially-written bundle file."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _invalidate_stale_bundle(path_str: str) -> None:
    """Issue #1648 fix_delta AC10: on a blocked materialization, delete any
    pre-existing bundle file at the target path so a previously-succeeded
    (now stale) bundle can never be mistaken for the result of this run."""
    path = Path(path_str)
    if path.exists():
        path.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize a receipt-bound TEST_VERDICT_MACHINE/v2 bundle")
    parser.add_argument("--publish-request-file", required=True)
    parser.add_argument("--command-hash-map-file", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--current-pr-number", type=int, required=True)
    parser.add_argument("--current-head-sha", required=True)
    parser.add_argument("--current-issue-body-sha256", required=True)
    parser.add_argument("--reviewed-head-sha", required=True)
    parser.add_argument("--diff-head-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--out-input-file", required=True)
    parser.add_argument("--out-private-file", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    publish_request, publish_request_errors = _load_json_file(args.publish_request_file)
    command_hash_map, command_hash_map_errors = _load_json_file(args.command_hash_map_file)

    preload_errors = publish_request_errors + command_hash_map_errors
    if preload_errors:
        _invalidate_stale_bundle(args.out_input_file)
        _invalidate_stale_bundle(args.out_private_file)
        result = _build_result("blocked", None, None, preload_errors)
        sys.stdout.write(_canonical_json(result) + "\n")
        return 1

    status, input_bundle, private_bundle, errors = materialize_test_verdict_artifact(
        publish_request=publish_request,
        command_hash_map=command_hash_map,
        repo=args.repo,
        issue_number=args.issue_number,
        current_pr_number=args.current_pr_number,
        current_head_sha=args.current_head_sha,
        current_issue_body_sha256=args.current_issue_body_sha256,
        reviewed_head_sha=args.reviewed_head_sha,
        diff_head_sha=args.diff_head_sha,
        run_id=args.run_id,
        run_url=args.run_url,
    )

    if status == "ok":
        _atomic_write(args.out_input_file, _canonical_json(input_bundle))
        _atomic_write(args.out_private_file, _canonical_json(private_bundle))
    else:
        _invalidate_stale_bundle(args.out_input_file)
        _invalidate_stale_bundle(args.out_private_file)

    result = _build_result(status, input_bundle, private_bundle, errors)
    sys.stdout.write(_canonical_json(result) + "\n")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
