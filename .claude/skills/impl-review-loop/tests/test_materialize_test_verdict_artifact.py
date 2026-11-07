"""E2E + negative regression tests for materialize_test_verdict_artifact.py.

Issue #1648 (Child C of #1645): exercises the full
execution record -> materializer -> receipt/private bundle -> adjudicator
chain, plus the negative cases the Issue body enumerates: missing receipt,
HEAD drift, artifact id/digest mismatch, and handwritten (self-attested,
non-materializer) TEST_VERDICT JSON.

Issue #1648 OWNER fix_delta (PR #1831 review): the materializer now performs
a live GitHub readback of the receipt's claimed workflow run/job/check run
and artifact (Issue #1647's `_verify_producer_run_and_job` /
`_verify_execution_artifact_metadata` / `_download_and_verify_artifact_archive`,
all in `scripts/agent-guards/controlled_skill_mutation_exec.py`). These
tests monkeypatch `subprocess.run` (shared across every module that does
`import subprocess; subprocess.run(...)`, including the dynamically
imported controlled_skill_mutation_exec module) to fixture a realistic
sequence of `gh api` responses -- including an actual zip archive built
with the `zipfile` module -- so the live readback path is genuinely
exercised end to end rather than merely bypassed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent / "scripts"
_MATERIALIZER_PATH = _SCRIPTS_DIR / "materialize_test_verdict_artifact.py"
_ADJUDICATOR_PATH = _SCRIPTS_DIR / "adjudicate_vc_result.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mat = _load_module(_MATERIALIZER_PATH, "materialize_test_verdict_artifact_under_test")
adj = _load_module(_ADJUDICATOR_PATH, "adjudicate_vc_result_under_test")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _digest_of_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


REPO = "squne121/agent-platform"
ISSUE_NUMBER = 1648
PR_NUMBER = 1830
HEAD_SHA = "a" * 40
ISSUE_BODY_SHA256 = "sha256:" + "b" * 64
COMMAND_HASH = "sha256:" + "c" * 64
ALL_ACS = ["AC1", "AC2", "AC3", "AC4", "AC5"]
GH_BIN = "/usr/bin/gh"


# -- Fixture builders ----------------------------------------------------- #


def _build_source() -> dict[str, Any]:
    return {
        "repository_id": 555,
        "repository_full_name": REPO,
        "commit_sha": HEAD_SHA,
        "tree_sha": "9" * 40,
        "execution_run_id": 1001,
        "execution_job_id": 2002,
    }


def _build_execution_record(**overrides: Any) -> dict[str, Any]:
    producer = {
        "workflow_path": ".github/workflows/ci.yml",
        "workflow_source_ref": "refs/heads/main",
        "workflow_source_sha": "d" * 40,
        "workflow_run_id": 1001,
        "workflow_run_attempt": 1,
        "job_id": 2002,
        "check_run_id": 3003,
    }
    subject = {
        "target_pr_number": PR_NUMBER,
        "head_repository_id": 555,
        "pr_head_sha": HEAD_SHA,
    }
    contract = {
        "linked_issue_number": ISSUE_NUMBER,
        "issue_body_sha256": ISSUE_BODY_SHA256,
        "command_manifest_sha256": "sha256:" + "e" * 64,
    }
    source = _build_source()
    executions = overrides.pop("executions", None) or [
        {
            "execution_id": "exec-1",
            "command_id": "uv.pytest.execution-record",
            "argv_sha256": "sha256:" + "f" * 64,
            "exit_code": 0,
            "status": "pass",
            "skipped": False,
            "fallback_detected": False,
            "timed_out": False,
            "stdout_sha256": "sha256:" + "0" * 64,
            "stderr_sha256": "sha256:" + "1" * 64,
        }
    ]
    per_ac = overrides.pop("per_ac", None) or [{"ac": ac, "execution_ids": ["exec-1"]} for ac in ALL_ACS]
    record: dict[str, Any] = {
        "schema": "TEST_VERDICT_EXECUTION_RECORD_V1",
        "schema_version": 1,
        "producer": producer,
        "subject": subject,
        "contract": contract,
        "source": source,
        "executions": executions,
        "per_ac": per_ac,
        "pass_eligible": True,
    }
    record.update(overrides)
    record["payload_sha256"] = _canonical_sha256(record)
    return record


def _build_artifact_zip_bytes(execution_record: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("execution-record.json", _canonical_json(execution_record))
    return buf.getvalue()


def _build_receipt(execution_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "TEST_VERDICT_PRODUCER_RECEIPT_V1",
        "schema_version": 1,
        "execution_payload_sha256": execution_record["payload_sha256"],
        "execution_artifact": {
            "artifact_id": 42,
            "artifact_url": "https://github.com/squne121/agent-platform/actions/runs/1001/artifacts/42",
            "artifact_archive_digest": "sha256:" + "2" * 64,
        },
        "producer": execution_record["producer"],
        "subject": execution_record["subject"],
        "contract": execution_record["contract"],
        "source": execution_record.get("source", _build_source()),
        "pass_eligible": True,
    }


def _build_publish_request(receipt: dict[str, Any]) -> dict[str, Any]:
    receipt_sha256 = _canonical_sha256(receipt)
    return {
        "schema": "TEST_VERDICT_PUBLISH_INPUT_V1",
        "issue_number": ISSUE_NUMBER,
        "repo": REPO,
        "target_pr_number": PR_NUMBER,
        "linked_issue_number": ISSUE_NUMBER,
        "expected_head_sha": HEAD_SHA,
        "linked_issue_body_sha256": ISSUE_BODY_SHA256,
        "producer_receipt": receipt,
        "receipt_sha256": receipt_sha256,
        "idempotency_key": f"{REPO}:{PR_NUMBER}:{ISSUE_NUMBER}:{HEAD_SHA}:{receipt_sha256}",
    }


def _build_command_hash_map() -> dict[str, str]:
    return {ac: COMMAND_HASH for ac in ALL_ACS}


def _valid_bundle(execution_record: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """Build a self-consistent (execution_record, receipt, zip_bytes) triple
    -- receipt.execution_artifact.artifact_archive_digest actually matches
    the sha256 of zip_bytes, and the record inside zip_bytes actually
    matches receipt.execution_payload_sha256."""
    execution_record = execution_record or _build_execution_record()
    zip_bytes = _build_artifact_zip_bytes(execution_record)
    receipt = _build_receipt(execution_record)
    receipt["execution_artifact"]["artifact_archive_digest"] = _digest_of_bytes(zip_bytes)
    return execution_record, receipt, zip_bytes


def _install_gh_fixture(monkeypatch: pytest.MonkeyPatch, receipt: dict[str, Any], zip_bytes: bytes) -> None:
    """Monkeypatch subprocess.run (shared with the dynamically imported
    controlled_skill_mutation_exec module -- Issue #1648 fix_delta AC6/AC8)
    to fixture the exact sequence of `gh api` calls the live readback
    functions make, plus gh binary discovery."""
    module, import_error = mat._load_controlled_exec_module()
    assert module is not None, import_error
    monkeypatch.setattr(module, "_find_gh_bin", lambda: (GH_BIN, ""))

    producer = receipt["producer"]
    subject = receipt["subject"]
    artifact = receipt["execution_artifact"]
    run_path = f"repos/{REPO}/actions/runs/{producer['workflow_run_id']}/attempts/{producer['workflow_run_attempt']}"
    job_path = f"repos/{REPO}/actions/jobs/{producer['job_id']}"
    check_path = f"repos/{REPO}/check-runs/{producer['check_run_id']}"
    artifact_meta_path = f"repos/{REPO}/actions/artifacts/{artifact['artifact_id']}"
    artifact_zip_path = f"{artifact_meta_path}/zip"

    responses = {
        run_path: json.dumps({"head_sha": subject["pr_head_sha"], "repository": {"full_name": REPO}}),
        job_path: json.dumps(
            {
                "run_id": producer["workflow_run_id"],
                "head_sha": subject["pr_head_sha"],
                "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{producer['check_run_id']}",
            }
        ),
        check_path: json.dumps({"id": producer["check_run_id"], "head_sha": subject["pr_head_sha"]}),
        artifact_meta_path: json.dumps({"id": artifact["artifact_id"], "expired": False}),
    }

    def _fake_run(cmd, **kwargs):
        path = cmd[-1]
        if path == artifact_zip_path:
            return SimpleNamespace(returncode=0, stdout=zip_bytes, stderr=b"")
        if path in responses:
            return SimpleNamespace(returncode=0, stdout=responses[path], stderr="")
        raise AssertionError(f"unexpected gh api path in test fixture: {path}")

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _materialize_kwargs(*, publish_request: dict[str, Any], command_hash_map: dict[str, Any] | None = None,
                         **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "publish_request": publish_request,
        "command_hash_map": command_hash_map if command_hash_map is not None else _build_command_hash_map(),
        "repo": REPO,
        "issue_number": ISSUE_NUMBER,
        "current_pr_number": PR_NUMBER,
        "current_head_sha": HEAD_SHA,
        "current_issue_body_sha256": ISSUE_BODY_SHA256,
        "reviewed_head_sha": HEAD_SHA,
        "diff_head_sha": HEAD_SHA,
        "run_id": "run-1001-1",
        "run_url": "https://github.com/squne121/agent-platform/actions/runs/1001",
        "gh_bin": GH_BIN,
        "gh_env": {},
    }
    kwargs.update(overrides)
    return kwargs


# -- AC1/AC6: current Issue/PR/HEAD/body SHA/artifact digest binding ------ #


def test_materializer_produces_bundle_when_all_bindings_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)

    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []
    assert input_bundle["schema"] == "TEST_VERDICT_MACHINE/v2"
    assert input_bundle["result"] == "PASS"
    assert len(input_bundle["runtime_ac_results"]) == len(ALL_ACS)
    assert all(item["status"] == "pass" for item in input_bundle["runtime_ac_results"])
    assert all(item["exit_code"] == 0 for item in input_bundle["runtime_ac_results"])
    assert input_bundle["producer_receipt"]["pass_eligible"] is True
    assert private_bundle["schema"] == "TEST_VERDICT_MATERIALIZE_PRIVATE_BUNDLE_V1"
    assert private_bundle["execution_record"] == execution_record
    assert "live_producer_run_and_job_verified" in private_bundle["verification_trace"]
    assert "artifact_archive_downloaded_and_digest_verified" in private_bundle["verification_trace"]


def test_materializer_blocks_on_current_head_sha_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request, current_head_sha="9" * 40)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert input_bundle is None
    assert private_bundle is None
    assert "current_head_sha_mismatch" in errors


def test_materializer_blocks_on_current_issue_body_sha256_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request, current_issue_body_sha256="sha256:" + "9" * 64)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert input_bundle is None
    assert "current_issue_body_sha256_mismatch" in errors


def test_materializer_blocks_on_current_pr_number_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request, current_pr_number=PR_NUMBER + 1)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "current_pr_number_mismatch" in errors


def test_materializer_blocks_on_current_issue_number_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request, issue_number=ISSUE_NUMBER + 1)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    # Child B's own field validator (reused verbatim) rejects this before the
    # materializer's own current-state binding checks run.
    assert "test_verdict_publish_linked_issue_number_mismatch" in errors


def test_materializer_blocks_on_publish_request_repo_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    publish_request["repo"] = "someone-else/other-repo"
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "test_verdict_publish_repo_mismatch" in errors


def test_materializer_blocks_on_producer_receipt_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    del receipt["execution_artifact"]["artifact_archive_digest"]  # required field missing
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert errors


def test_materializer_blocks_on_missing_command_hash_for_ac(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    command_hash_map = _build_command_hash_map()
    del command_hash_map["AC3"]
    kwargs = _materialize_kwargs(publish_request=publish_request, command_hash_map=command_hash_map)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "command_hash_map_missing_ac:AC3" in errors


# -- AC6 (P0-1): live GitHub readback ---------------------------------------- #


def test_materializer_blocks_on_live_readback_workflow_run_head_sha_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)

    module, _ = mat._load_controlled_exec_module()
    producer = receipt["producer"]
    run_path = f"repos/{REPO}/actions/runs/{producer['workflow_run_id']}/attempts/{producer['workflow_run_attempt']}"

    def _fake_run(cmd, **kwargs):
        path = cmd[-1]
        if path == run_path:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"head_sha": "f" * 40, "repository": {"full_name": REPO}}),
                stderr="",
            )
        raise AssertionError(f"unexpected gh api call before workflow run check: {path}")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert input_bundle is None
    assert any("workflow_run_head_sha_mismatch" in error for error in errors)


def test_materializer_blocks_on_live_readback_artifact_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)

    producer = receipt["producer"]
    subject = receipt["subject"]
    artifact = receipt["execution_artifact"]
    run_path = f"repos/{REPO}/actions/runs/{producer['workflow_run_id']}/attempts/{producer['workflow_run_attempt']}"
    job_path = f"repos/{REPO}/actions/jobs/{producer['job_id']}"
    check_path = f"repos/{REPO}/check-runs/{producer['check_run_id']}"
    artifact_meta_path = f"repos/{REPO}/actions/artifacts/{artifact['artifact_id']}"

    responses = {
        run_path: json.dumps({"head_sha": subject["pr_head_sha"], "repository": {"full_name": REPO}}),
        job_path: json.dumps(
            {
                "run_id": producer["workflow_run_id"],
                "head_sha": subject["pr_head_sha"],
                "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{producer['check_run_id']}",
            }
        ),
        check_path: json.dumps({"id": producer["check_run_id"], "head_sha": subject["pr_head_sha"]}),
        artifact_meta_path: json.dumps({"id": artifact["artifact_id"], "expired": True}),
    }

    def _fake_run(cmd, **kwargs):
        path = cmd[-1]
        if path in responses:
            return SimpleNamespace(returncode=0, stdout=responses[path], stderr="")
        raise AssertionError(f"unexpected gh api call after artifact expired: {path}")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert any("artifact_metadata_expired" in error for error in errors)


def test_materializer_blocks_on_live_readback_artifact_archive_digest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record, receipt, zip_bytes = _valid_bundle()
    # Leave the receipt's declared digest at its (wrong) placeholder value
    # instead of the actual zip_bytes digest.
    receipt["execution_artifact"]["artifact_archive_digest"] = "sha256:" + "8" * 64
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert any("artifact_archive_digest_mismatch" in error for error in errors)


def test_materializer_blocks_on_live_readback_artifact_archive_payload_self_check_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_record = _build_execution_record()
    # Build a zip whose contained record's self-reported payload_sha256 does
    # not match its own content (tampered after the correct value was
    # computed), while the outer archive digest is still self-consistent.
    tampered_record = dict(execution_record)
    tampered_record["payload_sha256"] = "sha256:" + "7" * 64
    zip_bytes = _build_artifact_zip_bytes(tampered_record)
    receipt = _build_receipt(execution_record)
    receipt["execution_artifact"]["artifact_archive_digest"] = _digest_of_bytes(zip_bytes)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert any("payload_self_check_failed" in error for error in errors)


def test_materializer_blocks_on_execution_record_receipt_digest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record()
    zip_bytes = _build_artifact_zip_bytes(execution_record)
    receipt = _build_receipt(execution_record)
    receipt["execution_artifact"]["artifact_archive_digest"] = _digest_of_bytes(zip_bytes)
    # The receipt claims a different execution payload than what the
    # (correctly self-consistent) downloaded archive actually contains.
    receipt["execution_payload_sha256"] = "sha256:" + "6" * 64
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert any("payload_sha256_mismatch" in error for error in errors)


# -- AC7 (P1-1): strict exit_code / skipped / timed_out verification ------- #


def test_materializer_ac_fails_on_nonzero_exit_code_despite_pass_status(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record(
        executions=[
            {
                "execution_id": "exec-1",
                "command_id": "uv.pytest.execution-record",
                "argv_sha256": "sha256:" + "f" * 64,
                "exit_code": 1,
                "status": "pass",
                "skipped": False,
                "fallback_detected": False,
                "timed_out": False,
                "stdout_sha256": "sha256:" + "0" * 64,
                "stderr_sha256": "sha256:" + "1" * 64,
            }
        ],
    )
    _, receipt, zip_bytes = _valid_bundle(execution_record)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "ok"
    assert errors == []
    assert input_bundle["result"] == "FAIL"
    failing = [item for item in input_bundle["runtime_ac_results"] if item["status"] == "fail"]
    assert len(failing) == len(ALL_ACS)
    # Issue #1648 fix_delta AC7: the actual exit_code (1) is reflected, not
    # unconditionally normalized to some other fixed value.
    assert all(item["exit_code"] == 1 for item in failing)


def test_materializer_ac_fails_on_skipped_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record(
        executions=[
            {
                "execution_id": "exec-1",
                "command_id": "uv.pytest.execution-record",
                "argv_sha256": "sha256:" + "f" * 64,
                "exit_code": 0,
                "status": "pass",
                "skipped": True,
                "fallback_detected": False,
                "timed_out": False,
                "stdout_sha256": "sha256:" + "0" * 64,
                "stderr_sha256": "sha256:" + "1" * 64,
            }
        ],
    )
    _, receipt, zip_bytes = _valid_bundle(execution_record)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "ok"
    assert input_bundle["result"] == "FAIL"
    assert all(item["status"] == "fail" for item in input_bundle["runtime_ac_results"])


def test_materializer_ac_fails_on_timed_out_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record(
        executions=[
            {
                "execution_id": "exec-1",
                "command_id": "uv.pytest.execution-record",
                "argv_sha256": "sha256:" + "f" * 64,
                "exit_code": 0,
                "status": "pass",
                "skipped": False,
                "fallback_detected": False,
                "timed_out": True,
                "stdout_sha256": "sha256:" + "0" * 64,
                "stderr_sha256": "sha256:" + "1" * 64,
            }
        ],
    )
    _, receipt, zip_bytes = _valid_bundle(execution_record)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "ok"
    assert input_bundle["result"] == "FAIL"
    assert all(item["status"] == "fail" for item in input_bundle["runtime_ac_results"])


def test_materializer_blocks_on_duplicate_execution_id(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record(
        executions=[
            {
                "execution_id": "exec-1",
                "command_id": "uv.pytest.execution-record",
                "argv_sha256": "sha256:" + "f" * 64,
                "exit_code": 0,
                "status": "pass",
                "skipped": False,
                "fallback_detected": False,
                "timed_out": False,
                "stdout_sha256": "sha256:" + "0" * 64,
                "stderr_sha256": "sha256:" + "1" * 64,
            },
            {
                "execution_id": "exec-1",
                "command_id": "uv.pytest.execution-record-dup",
                "argv_sha256": "sha256:" + "f" * 64,
                "exit_code": 0,
                "status": "pass",
                "skipped": False,
                "fallback_detected": False,
                "timed_out": False,
                "stdout_sha256": "sha256:" + "0" * 64,
                "stderr_sha256": "sha256:" + "1" * 64,
            },
        ],
    )
    _, receipt, zip_bytes = _valid_bundle(execution_record)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert "execution_record_execution_id_duplicate:exec-1" in errors


def test_materializer_blocks_on_missing_per_ac_entry_for_known_ac(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record(
        per_ac=[{"ac": ac, "execution_ids": ["exec-1"]} for ac in ALL_ACS if ac != "AC5"],
    )
    _, receipt, zip_bytes = _valid_bundle(execution_record)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert any("execution_record_per_ac_missing_acs" in error and "AC5" in error for error in errors)


def test_materializer_blocks_on_unreferenced_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record(
        executions=[
            {
                "execution_id": "exec-1",
                "command_id": "uv.pytest.execution-record",
                "argv_sha256": "sha256:" + "f" * 64,
                "exit_code": 0,
                "status": "pass",
                "skipped": False,
                "fallback_detected": False,
                "timed_out": False,
                "stdout_sha256": "sha256:" + "0" * 64,
                "stderr_sha256": "sha256:" + "1" * 64,
            },
            {
                "execution_id": "exec-orphan",
                "command_id": "uv.pytest.orphan",
                "argv_sha256": "sha256:" + "f" * 64,
                "exit_code": 0,
                "status": "pass",
                "skipped": False,
                "fallback_detected": False,
                "timed_out": False,
                "stdout_sha256": "sha256:" + "0" * 64,
                "stderr_sha256": "sha256:" + "1" * 64,
            },
        ],
    )
    _, receipt, zip_bytes = _valid_bundle(execution_record)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    kwargs = _materialize_kwargs(publish_request=publish_request)
    status, input_bundle, private_bundle, errors = mat.materialize_test_verdict_artifact(**kwargs)
    assert status == "blocked"
    assert any(
        "execution_record_unreferenced_executions" in error and "exec-orphan" in error for error in errors
    )


# -- AC8 (P1-2): connected E2E execution record -> materializer -> adjudicator -- #


def _build_pr_review_only_results() -> list[dict[str, Any]]:
    return [
        {
            "ac": ac,
            "command_hash": COMMAND_HASH,
            "exit_code": None,
            "runner": "skipped",
            "classification": "skipped",
            "category": "preflight_scope_pr_review_only",
            "decision": "go",
            "scope_class": "pr_review_only",
            "verification_owner": "pr-review-judge",
            "deferred_reason": "VC marked pr_review_only; verification deferred to PR review",
            "runtime_verification_required": False,
        }
        for ac in ALL_ACS
    ]


def _build_adjudication_context() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    results = _build_pr_review_only_results()
    contract_snapshot = {
        "schema": "baseline_vc_preflight/v1",
        "status": "go",
        "body_sha256": ISSUE_BODY_SHA256,
        "results": results,
    }
    current_vc_result = {
        "schema": "baseline_vc_preflight/v1",
        "issue": ISSUE_NUMBER,
        "generated_at": "2026-07-28T00:00:00Z",
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": ISSUE_BODY_SHA256},
        "head_sha": HEAD_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "results": results,
    }
    diff_summary = {
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "base_sha": "0" * 40,
        "changed_paths": ["foo.py"],
    }
    allowed_paths = ["foo.py"]
    return contract_snapshot, current_vc_result, diff_summary, allowed_paths


def test_e2e_execution_record_to_materializer_to_adjudicator_resolves_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)

    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=input_bundle,
        require_producer_receipt=True,
    )
    assert result["errors"] == []
    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    acs_covered = {item["ac"] for item in result["per_ac"]}
    assert acs_covered == set(ALL_ACS)


# -- AC2: adjudicator fail-closed negative cases ------------------------------


def _manual_handwritten_test_verdict() -> dict[str, Any]:
    """A fully self-attested TEST_VERDICT (Issue #1648: never produced by the
    materializer, no producer_receipt at all) -- internally consistent but
    carries zero Child A/B provenance."""
    artifact_payload = {
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "diff_head_sha": HEAD_SHA,
        "contract_body_sha256": ISSUE_BODY_SHA256,
        "command_hashes": sorted([COMMAND_HASH] * len(ALL_ACS)),
    }
    return {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "producer_kind": "test-runner",
        "repository": REPO,
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "diff_head_sha": HEAD_SHA,
        "contract_body_sha256": ISSUE_BODY_SHA256,
        "run_id": "handwritten-1",
        "run_url": "https://example.invalid/runs/handwritten-1",
        "workflow_run_id": 1,
        "workflow_run_attempt": 1,
        "check_run_id": 1,
        "artifact": {
            "name": "test-verdict-machine",
            "artifact_digest": "sha256:" + "3" * 64,
            "url": "https://github.com/squne121/agent-platform/actions/runs/1/artifacts/1",
        },
        "artifact_payload": artifact_payload,
        "artifact_payload_sha256": _canonical_sha256(artifact_payload),
        "result": "PASS",
        "verification_commands_pass": len(ALL_ACS),
        "verification_commands_fail": 0,
        "verification_skipped_count": 0,
        "runtime_ac_results": [
            {
                "ac": ac,
                "command_hash": COMMAND_HASH,
                "exit_code": 0,
                "status": "pass",
                "fallback_detected": False,
                "human_review_required": False,
                "stop_condition_triggered": False,
            }
            for ac in ALL_ACS
        ],
    }


def test_adjudicator_rejects_handwritten_test_verdict_with_no_receipt_when_required() -> None:
    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=_manual_handwritten_test_verdict(),
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_producer_receipt_missing" in result["errors"]


def test_adjudicator_accepts_handwritten_test_verdict_when_receipt_not_required() -> None:
    """Non-regression: the legacy self-attested TEST_VERDICT path (default
    require_producer_receipt=False, and the input carries no producer_receipt
    field at all) is left unchanged so Step 2 / test-runner callers that have
    not yet migrated to the materializer keep working (Issue #1648 body
    compatibility note; Issue #1648 fix_delta AC9 auto-force only triggers
    when a producer_receipt field is actually present)."""
    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=_manual_handwritten_test_verdict(),
    )
    assert result["overall_status"] == "pass"
    assert result["blocking"] is False


def test_adjudicator_rejects_materialized_bundle_missing_receipt_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []
    tampered = dict(input_bundle)
    del tampered["producer_receipt"]

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_producer_receipt_missing" in result["errors"]


def test_adjudicator_rejects_head_drift_between_materialized_bundle_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["head_sha"] = "7" * 40
    tampered["reviewed_head_sha"] = "7" * 40
    tampered["diff_head_sha"] = "7" * 40
    tampered["artifact_payload"]["head_sha"] = "7" * 40
    tampered["artifact_payload"]["reviewed_head_sha"] = "7" * 40
    tampered["artifact_payload"]["diff_head_sha"] = "7" * 40
    tampered["artifact_payload_sha256"] = _canonical_sha256(tampered["artifact_payload"])

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    current_vc_result["head_sha"] = "7" * 40
    current_vc_result["reviewed_head_sha"] = "7" * 40
    diff_summary["head_sha"] = "7" * 40

    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_subject_head_sha_mismatch" in result["errors"]


def test_adjudicator_rejects_artifact_digest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["artifact"]["artifact_digest"] = "sha256:" + "4" * 64

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_artifact_digest_mismatch" in result["errors"]


def test_adjudicator_rejects_receipt_sha256_tamper(monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["receipt_sha256"] = "sha256:" + "5" * 64

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_sha256_mismatch" in result["errors"]


def test_adjudicator_rejects_receipt_not_pass_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    execution_record = _build_execution_record()
    _, receipt, zip_bytes = _valid_bundle(execution_record)
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["producer_receipt"]["pass_eligible"] = False
    # Recompute receipt_sha256 so the pass_eligible check -- not the sha256
    # binding check -- is what rejects this tampered receipt.
    tampered["receipt_sha256"] = _canonical_sha256(tampered["producer_receipt"])

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        require_producer_receipt=True,
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_not_pass_eligible" in result["errors"]


# -- AC9 (P1-3): auto-forced receipt verification when unflagged ------------- #


def test_adjudicator_auto_forces_receipt_verification_without_flag_when_bundle_has_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A materialized bundle (which always carries producer_receipt) must be
    receipt-verified even if the caller forgets --require-producer-receipt.
    Here the receipt is tampered, so the omitted-flag call must still
    fail-closed instead of silently falling back to the legacy self-attested
    (no receipt check) path."""
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []
    tampered = json.loads(json.dumps(input_bundle))
    tampered["receipt_sha256"] = "sha256:" + "5" * 64

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=tampered,
        # require_producer_receipt intentionally omitted (defaults False).
    )
    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "test_verdict_receipt_sha256_mismatch" in result["errors"]


def test_adjudicator_auto_forced_receipt_verification_accepts_valid_bundle_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    status, input_bundle, _private_bundle, errors = mat.materialize_test_verdict_artifact(
        **_materialize_kwargs(publish_request=publish_request)
    )
    assert status == "ok"
    assert errors == []

    contract_snapshot, current_vc_result, diff_summary, allowed_paths = _build_adjudication_context()
    result = adj.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
        test_verdict=input_bundle,
        # require_producer_receipt intentionally omitted (defaults False) --
        # producer_receipt presence alone must be sufficient to trigger and
        # pass full receipt verification.
    )
    assert result["overall_status"] == "pass"
    assert result["blocking"] is False


# -- AC10 (P1-4): atomic bundle write / stale-bundle invalidation ----------- #


def test_atomic_write_produces_final_file_without_temp_residue(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "bundle.json"
    mat._atomic_write(str(target), '{"a": 1}')
    assert target.exists()
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    leftover = [p for p in target.parent.iterdir() if p != target]
    assert leftover == []


def test_invalidate_stale_bundle_removes_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "bundle.json"
    target.write_text("stale", encoding="utf-8")
    mat._invalidate_stale_bundle(str(target))
    assert not target.exists()


def test_invalidate_stale_bundle_noop_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "does-not-exist.json"
    mat._invalidate_stale_bundle(str(target))  # must not raise
    assert not target.exists()


def test_main_cli_invalidates_stale_bundle_on_blocked_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, receipt, zip_bytes = _valid_bundle()
    _install_gh_fixture(monkeypatch, receipt, zip_bytes)
    publish_request = _build_publish_request(receipt)
    command_hash_map = _build_command_hash_map()

    publish_request_file = tmp_path / "publish_request.json"
    command_hash_map_file = tmp_path / "command_hash_map.json"
    out_input_file = tmp_path / "out" / "input.json"
    out_private_file = tmp_path / "out" / "private.json"

    publish_request_file.write_text(_canonical_json(publish_request), encoding="utf-8")
    command_hash_map_file.write_text(_canonical_json(command_hash_map), encoding="utf-8")

    argv = [
        "--publish-request-file", str(publish_request_file),
        "--command-hash-map-file", str(command_hash_map_file),
        "--repo", REPO,
        "--issue-number", str(ISSUE_NUMBER),
        "--current-pr-number", str(PR_NUMBER),
        "--current-head-sha", HEAD_SHA,
        "--current-issue-body-sha256", ISSUE_BODY_SHA256,
        "--reviewed-head-sha", HEAD_SHA,
        "--diff-head-sha", HEAD_SHA,
        "--run-id", "run-1001-1",
        "--run-url", "https://github.com/squne121/agent-platform/actions/runs/1001",
        "--out-input-file", str(out_input_file),
        "--out-private-file", str(out_private_file),
    ]

    exit_code = mat.main(argv)
    assert exit_code == 0
    assert out_input_file.exists()
    assert out_private_file.exists()

    tampered = dict(publish_request)
    tampered["repo"] = "someone-else/other-repo"
    publish_request_file.write_text(_canonical_json(tampered), encoding="utf-8")

    exit_code2 = mat.main(argv)
    assert exit_code2 == 1
    assert not out_input_file.exists()
    assert not out_private_file.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
