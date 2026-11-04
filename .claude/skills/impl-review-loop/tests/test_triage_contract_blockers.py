from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT_PATH = HERE.parent / "scripts" / "triage_contract_blockers.py"
PREPARATION_MD = HERE.parent / "steps" / "preparation.md"

spec = importlib.util.spec_from_file_location("triage_contract_blockers", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def run_cli(payload: object) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def make_item(
    ac: str,
    category: str,
    *,
    command_hash: str = "sha256:" + "a" * 64,
    exit_code: int = 5,
    raw_command: str | None = None,
    runner_env_delta: dict | None = None,
    subreason: str | None = None,
) -> dict:
    item = {
        "ac": ac,
        "command_hash": command_hash,
        "category": category,
        "decision": "blocked",
        "exit_code": exit_code,
        "runner_env_delta": runner_env_delta or {},
    }
    if raw_command is not None:
        item["raw_command"] = raw_command
    if subreason is not None:
        item["subreason"] = subreason
    return item


def test_accepts_actual_baseline_producer_schema():
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC1",
                "vc_no_tests_collected",
                subreason="pytest_k_filter_matches_no_tests",
                raw_command="uv run pytest tests/foo.py -k mismatch -v",
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok"
    assert result["source_integrity"]["input_schema"] == "baseline_vc_preflight/v1"


def test_structured_contract_review_result_input_is_accepted():
    payload = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "checks": {
            "vc_preflight": {
                "classifications": [
                    make_item(
                        "AC2",
                        "vc_no_tests_collected",
                        subreason="pytest_k_filter_matches_no_tests",
                    )
                ]
            }
        },
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok"
    assert result["per_ac"][0]["subreason"] == "pytest_k_filter_matches_no_tests"


def test_scalar_only_contract_review_result_is_unsupported():
    payload = {"schema": "CONTRACT_REVIEW_RESULT_V1", "checks": {"vc_preflight": "blocked"}}
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "unsupported_input"
    assert result["source_integrity"]["unsupported_reason"] == "scalar_vc_preflight_only"


def test_latest_blocked_snapshot_only_is_unsupported():
    payload = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "source": "latest_blocked",
        "contract_snapshot_url": "https://example.invalid/comment",
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "unsupported_input"
    assert result["source_integrity"]["unsupported_reason"] == "latest_blocked_snapshot_only"


def test_empty_classifications_is_incomplete_evidence():
    payload = {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": "blocked",
        "vc_preflight_classifications": [],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "incomplete_evidence"
    assert result["source_integrity"]["evidence_complete"] is False


def test_pnpm_retry_uses_argv_and_env_delta():
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [make_item("AC3", "package_manager_no_tty_prompt", exit_code=1)],
    }
    result = mod.triage_contract_blockers(payload)
    action = result["suggested_actions"][0]
    assert action["kind"] == "retry_with_runner_env_delta"
    assert action["argv"] == ["pnpm", "build"]
    assert action["env_delta"] == {"CI": "true"}


def test_pnpm_with_ci_true_routes_to_inspection():
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC4",
                "package_manager_no_tty_prompt",
                exit_code=1,
                runner_env_delta={"CI": "true"},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["environment_retry_recommended"] is False
    assert result["suggested_actions"][0]["kind"] == "inspect_package_manager_state"


def test_command_hash_is_preserved_without_raw_command_echo():
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC5",
                "vc_no_tests_collected",
                command_hash="sha256:" + "b" * 64,
                subreason="pytest_k_filter_matches_no_tests",
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["per_ac"][0]["command_hash"] == "sha256:" + "b" * 64
    assert "command" not in result["per_ac"][0]


def test_malformed_item_is_incomplete_not_silently_dropped():
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": ["not-an-object"],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "incomplete_evidence"
    assert "non_object_item" in result["errors"]


def test_top_level_null_returns_machine_json():
    exit_code, result = run_cli(None)
    assert exit_code == 1
    assert result["status"] == "invalid_input"


def test_top_level_list_returns_machine_json():
    exit_code, result = run_cli([])
    assert exit_code == 1
    assert result["status"] == "invalid_input"


def test_preparation_summary():
    """#1851: the missing_contract_go-gated triage normalizer subsection was removed from
    preparation.md (contract_snapshot_url is optional telemetry, not a blocking gate)."""
    body = PREPARATION_MD.read_text(encoding="utf-8")
    assert "missing_contract_go" not in body
