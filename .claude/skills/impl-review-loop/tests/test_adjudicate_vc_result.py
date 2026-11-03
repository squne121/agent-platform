from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "adjudicate_vc_result.py"
)


spec = importlib.util.spec_from_file_location("adjudicate_vc_result", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _payload_item(
    ac: str,
    *,
    command_hash: str = "sha256:" + "a" * 64,
    failure_keys: list[dict[str, str]] | None = None,
    exit_code: int = 1,
    category: str = "regression_gate",
    raw_command: str = "pytest tests/test_alpha.py",
) -> dict:
    return {
        "ac": ac,
        "command_hash": command_hash,
        "failure_keys": failure_keys
        if failure_keys is not None
        else [{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
        "exit_code": exit_code,
        "category": category,
        "classification": "expected_fail",
        "decision": "blocked",
        "raw_command": raw_command,
        "raw_stdout": "pytest output",
        "raw_stderr": "error",
    }


def _snapshot_payload(items: list[dict[str, object]]) -> dict:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": "sha256:" + "b" * 64,
        "checks": {
            "vc_preflight": {
                "classifications": items,
            }
        },
    }


def _current_payload(
    items: list[dict[str, object]],
    *,
    head_sha: str | None = None,
    reviewed_head_sha: str | None = None,
) -> dict:
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "generated_at": "2026-07-11T10:00:00Z",
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": "sha256:" + "b" * 64},
        "results": items,
    }
    if head_sha is not None:
        payload["head_sha"] = head_sha
    if reviewed_head_sha is not None:
        payload["reviewed_head_sha"] = reviewed_head_sha
    return payload


def _allowed_paths_file(tmp_path: Path, paths: list[str]) -> Path:
    path = tmp_path / "allowed_paths.json"
    path.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    return path


def test_no_diff_same_baseline_failure_is_pre_existing_nonblocking():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "pre_existing_fail"
    assert result["blocking"] is False
    assert result["rerun_required"] is False
    assert result["per_ac"][0]["status"] == "pre_existing_fail"


def test_ac3_failure_key_evidence_missing_becomes_indeterminate_blocking():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload(
        [_payload_item("AC1", command_hash="sha256:" + "a" * 64, failure_keys=[])],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["per_ac"][0]["status"] == "indeterminate"


def test_diff_related_failure_is_regression_blocking():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "src/main.py::test_regression"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["src/**"],
    )

    assert result["overall_status"] == "regression_fail"
    assert result["blocking"] is True
    assert result["per_ac"][0]["status"] == "regression_fail"


def test_allowed_paths_glob_matcher_v2_relevance_is_regression_blocking():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[
                    {
                        "kind": "pytest_nodeid",
                        "key": ".claude/skills/impl-review-loop/tests/test_adjudicate_vc_result.py::test_case",
                    }
                ],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["docs/dev/agent-run-report.md"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "regression_fail"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "related_to_changed_scope"


def test_pytest_exit_5_is_not_regression_fail():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload(
            [_payload_item("AC1", command_hash="sha256:" + "a" * 64, exit_code=5, category="vc_no_tests_collected")],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] in {"indeterminate", "environment_blocked"}
    assert result["per_ac"][0]["status"] != "regression_fail"


def test_current_head_mismatch_is_indeterminate_blocking():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload(
        [_payload_item("AC1")],
        head_sha="old-head",
        reviewed_head_sha="old-head",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "new-head"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["source_integrity"]["evidence_fresh"] is False


def test_mixed_expected_baseline_results_with_certified_current_pass_are_nonblocking():
    items = [
        _payload_item(
            f"AC{index}",
            command_hash="sha256:" + f"{index:x}" * 64,
            failure_keys=[],
            exit_code=4 if index not in {6, 7} else 0,
        )
        for index in range(1, 9)
    ]
    for item in items:
        item["classification"] = "expected_pass" if item["ac"] in {"AC6", "AC7"} else "expected_fail"
    current_items = [
        {
            **item,
            "exit_code": 0,
            "failure_keys": [],
        }
        for item in items
    ]
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload(items),
        current_vc_result=_current_payload(current_items, head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
    )
    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert {entry["reason_code"] for entry in result["per_ac"]} == {
        "expected_fail_resolved_on_current_head",
        "expected_pass_still_passes",
    }


def test_current_pass_requires_complete_certified_envelope_and_allowed_paths():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    current = _current_payload([current_item], head_sha="head1", reviewed_head_sha="head1")
    current["errors"] = ["producer_error"]
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/outside.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "uncertified_current_pass"


def test_mapping_and_invalid_paths_fail_closed_for_current_pass():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [
                {
                    "path": ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py",
                    "previous_path": "../outside.py",
                }
            ],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/**"],
    )
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "uncertified_current_pass"

    current_item["ac"] = "AC2"
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["errors"] == ["baseline_current_mapping_mismatch"]


def test_bool_exit_code_and_pass_with_failure_keys_fail_closed():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    invalid = _payload_item("AC1", failure_keys=[], exit_code=False)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([invalid], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["blocking"] is True
    assert result["errors"] == ["current[0]:invalid_exit_code"]

    invalid["exit_code"] = 0
    invalid["failure_keys"] = [{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}]
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([invalid], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["per_ac"][0]["reason_code"] == "pass_with_failure_keys"


def test_new_unrelated_failure_with_diff_is_not_out_of_scope():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "docs/readme.md::test_readme"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "new_failure_without_scope_proof"


def test_out_of_scope_requires_diff_and_failure_key_evidence():
    test_new_unrelated_failure_with_diff_is_not_out_of_scope()


def test_failure_key_kind_process_exit_cannot_prove_out_of_scope():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "process_exit", "key": "process_exit:1"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["src/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "unsupported_failure_key_kind"


def test_invalid_allowed_path_pattern_is_indeterminate():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["docs/**/*.md"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert any(error.startswith("invalid_allowed_path_pattern:") for error in result["errors"])


def test_empty_current_classifications_without_pass_signal_is_indeterminate():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([], head_sha="head1", reviewed_head_sha="head1")

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["errors"] == ["empty_current_results_without_pass_signal"]


def test_full_stdout_stderr_only_in_private_artifact(tmp_path: Path):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")

    allowed_path_file = _allowed_paths_file(tmp_path, [".claude/skills/**"])
    contract_file = tmp_path / "contract.json"
    current_file = tmp_path / "current.json"
    diff_file = tmp_path / "diff.json"
    artifact_file = tmp_path / "artifact.json"

    contract_file.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    diff_file.write_text(
        json.dumps({"changed_paths": [], "head_sha": "head1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--contract-snapshot-file",
            str(contract_file),
            "--current-vc-result-file",
            str(current_file),
            "--diff-summary-file",
            str(diff_file),
            "--allowed-paths-file",
            str(allowed_path_file),
            "--artifact-out",
            str(artifact_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    compact = json.loads(result.stdout)
    compact_text = json.dumps(compact)
    assert "raw_stdout" not in compact_text
    assert "raw_stderr" not in compact_text
    assert "full_command_output" not in compact_text
    assert compact["artifact_ref"] == "vc-adjudication-private-bundle"
    assert compact["artifact_digest"].startswith("sha256:")

    private = json.loads(artifact_file.read_text(encoding="utf-8"))
    assert private["schema"] == "VC_ADJUDICATION_PRIVATE_BUNDLE_V1"
    assert private["current_vc_result"]["results"][0]["raw_stdout"] == "pytest output"


def test_compact_result_does_not_emit_local_artifact_path(tmp_path: Path):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")
    allowed_path_file = _allowed_paths_file(tmp_path, [".claude/skills/**"])
    contract_file = tmp_path / "contract.json"
    current_file = tmp_path / "current.json"
    diff_file = tmp_path / "diff.json"
    artifact_file = tmp_path / "nested" / "artifact.json"
    artifact_file.parent.mkdir()

    contract_file.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    diff_file.write_text(
        json.dumps({"changed_paths": [], "head_sha": "head1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--contract-snapshot-file",
            str(contract_file),
            "--current-vc-result-file",
            str(current_file),
            "--diff-summary-file",
            str(diff_file),
            "--allowed-paths-file",
            str(allowed_path_file),
            "--artifact-out",
            str(artifact_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    compact = json.loads(result.stdout)
    assert compact["artifact_ref"] == "vc-adjudication-private-bundle"
    assert str(artifact_file) not in result.stdout


def test_compact_stdout_remains_valid_json_when_truncated_budget_is_small(tmp_path: Path):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "src/main.py::test_regression"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )
    allowed_path_file = _allowed_paths_file(tmp_path, ["src/**"])
    contract_file = tmp_path / "contract.json"
    current_file = tmp_path / "current.json"
    diff_file = tmp_path / "diff.json"

    contract_file.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    diff_file.write_text(
        json.dumps({"changed_paths": ["src/main.py"], "head_sha": "head1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--contract-snapshot-file",
            str(contract_file),
            "--current-vc-result-file",
            str(current_file),
            "--diff-summary-file",
            str(diff_file),
            "--allowed-paths-file",
            str(allowed_path_file),
            "--max-stdout-bytes",
            "200",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    compact = json.loads(result.stdout)
    assert compact["stdout_truncated"] is True
    assert compact["overall_status"] == "indeterminate"
    assert compact["errors"] == ["stdout_budget_exceeded"]


def test_artifact_digest_is_canonical_and_stable_across_key_order():
    payload_a = {"a": 1, "b": 2}
    payload_b = {"b": 2, "a": 1}
    assert mod._sha256(mod._canonical_json(payload_a)) == mod._sha256(mod._canonical_json(payload_b))


def test_bare_legacy_command_hash_is_either_normalized_or_rejected_with_migration_reason():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                command_hash="a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["per_ac"][0]["command_hash"] == "sha256:" + "a" * 64
    assert result["per_ac"][0]["command_hash_note"] == "normalized_legacy_bare_command_hash"


def test_allowed_paths_matcher_import_failure_returns_machine_json(monkeypatch):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")

    def _fail():
        return None, "allowed_paths_matcher_import_failed:ImportError"

    monkeypatch.setattr(mod, "_get_allowed_paths_matcher", _fail)

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["src/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert any(error.startswith("allowed_paths_matcher_import_failed:") for error in result["errors"])


def test_github_standard_filename_key_outside_allowed_paths_blocks_current_pass():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [{"filename": "src/outside.ts", "status": "modified"}],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "uncertified_current_pass"


def test_empty_changed_paths_does_not_vacuously_certify_current_pass():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "uncertified_current_pass"


def test_github_standard_filename_key_inside_allowed_paths_still_certifies_current_pass():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [
                {
                    "filename": ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py",
                    "status": "modified",
                }
            ],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"


def test_unrecognized_changed_path_record_is_fail_closed_indeterminate():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={
            "changed_paths": [{"status": "modified", "sha": "abc123"}],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert "unrecognized_changed_path_record" in result["errors"]


# --- Issue #88 AC2/AC3: Step 4 current-head gate binding triple ---
# evaluate_step4_vc_gate() re-derives a fail-closed decision from an
# existing VC_ADJUDICATION_RESULT_V1 (produced by adjudicate_vc_result()
# above) plus the caller-supplied "expected" (live) head SHA / contract body
# SHA256 / command hashes triple.


def test_ac2_step4_gate_accepts_matching_binding_triple():
    # GIVEN a certified current-head PASS adjudication for a single AC
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
    )
    assert result["blocking"] is False

    # WHEN Step 4 evaluates the gate against the exact live (head, body, commands) triple
    decision = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha="head1",
        expected_contract_body_sha256="sha256:" + "b" * 64,
        expected_command_hashes=[current_item["command_hash"]],
    )

    # THEN pr-reviewer may be invoked
    assert decision["invoke_pr_reviewer"] is True
    assert decision["reason_code"] is None


def test_ac3_step4_gate_rejects_head_drift():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
    )
    assert result["blocking"] is False

    # WHEN the live PR head has since drifted away from the adjudicated head
    decision = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha="head2-drifted",
        expected_contract_body_sha256="sha256:" + "b" * 64,
        expected_command_hashes=[current_item["command_hash"]],
    )

    # THEN the stale adjudication does not open the gate
    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "head_mismatch"


def test_ac3_step4_gate_rejects_body_drift():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
    )
    assert result["blocking"] is False

    # WHEN the live Issue body has since been edited (different body_sha256)
    decision = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha="head1",
        expected_contract_body_sha256="sha256:" + "c" * 64,
        expected_command_hashes=[current_item["command_hash"]],
    )

    # THEN the stale adjudication does not open the gate
    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "body_mismatch"


def test_ac3_step4_gate_rejects_command_drift():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
    )
    assert result["blocking"] is False

    # WHEN the live Verification Commands have since changed (different literal command hash)
    decision = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha="head1",
        expected_contract_body_sha256="sha256:" + "b" * 64,
        expected_command_hashes=["sha256:" + "9" * 64],
    )

    # THEN the stale adjudication does not open the gate
    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "command_mismatch"


# --- Issue #88 fix_delta Blocker 1: canonical adapter -----------------------
# (TEST_VERDICT_MACHINE/v2 -> baseline_vc_preflight/v1, and the `adapt` /
# `step4-gate` CLI subcommands wired to LOOP_STATE.vc_adjudication)


def _sample_test_verdict() -> dict:
    return {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "producer_kind": "test-runner",
        "repository": "squne121/agent-platform",
        "issue_number": 88,
        "pr_number": 2312,
        "head_sha": "a" * 40,
        "reviewed_head_sha": "a" * 40,
        "diff_head_sha": "a" * 40,
        "contract_body_sha256": "sha256:" + "b" * 64,
        "generated_at": "2026-08-23T00:00:00Z",
        "result": "PASS",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "branch_behind_main": False,
        "baseline_only": False,
        "verification_commands_pass": 1,
        "verification_commands_fail": 0,
        "verification_skipped_count": 0,
        "runtime_ac_results": [
            {
                "ac": "AC1",
                "command": "pnpm test",
                "command_hash": "sha256:" + "c" * 64,
                "exit_code": 0,
                "status": "pass",
                "fallback_detected": False,
                "artifact_present": "not_required",
                "human_review_required": False,
                "stop_condition_triggered": False,
                "notes": "",
            }
        ],
    }


def test_adapt_converts_pass_report_to_baseline_vc_preflight_shape():
    converted, errors = mod.adapt_test_verdict_to_current_vc_result(_sample_test_verdict())

    assert errors == []
    assert converted is not None
    assert converted["schema"] == "baseline_vc_preflight/v1"
    assert converted["head_sha"] == "a" * 40
    assert converted["reviewed_head_sha"] == "a" * 40
    assert converted["source"]["body_sha256"] == "sha256:" + "b" * 64
    assert converted["results"] == [
        {
            "ac": "AC1",
            "command_hash": "sha256:" + "c" * 64,
            "raw_command": "pnpm test",
            "exit_code": 0,
            "status": "pass",
            "fallback_detected": False,
            "human_review_required": False,
            "stop_condition_triggered": False,
            "failure_keys": [],
            "raw_stdout": "",
            "raw_stderr": "",
        }
    ]
    assert converted["fallback_detected"] is False
    assert converted["human_review_required"] is False
    # Issue #2467 P1 review fix: payload-level identity fields must be
    # carried through losslessly (not dropped by the adapter).
    assert converted["issue"] == 88
    assert converted["pr_number"] == 2312
    assert converted["diff_head_sha"] == "a" * 40


def test_adapt_rejects_wrong_source_schema():
    converted, errors = mod.adapt_test_verdict_to_current_vc_result({"schema": "SOMETHING_ELSE"})

    assert converted is None
    assert errors == ["unsupported_source_schema:'SOMETHING_ELSE'"]


def test_adapt_unwraps_test_verdict_key():
    wrapped = {"TEST_VERDICT": _sample_test_verdict()}
    converted, errors = mod.adapt_test_verdict_to_current_vc_result(wrapped)

    assert errors == []
    assert converted is not None
    assert converted["head_sha"] == "a" * 40


def test_adapted_output_feeds_adjudicate_vc_result_as_a_certified_pass():
    command_hash = "sha256:" + "c" * 64
    body_sha256 = "sha256:" + "b" * 64
    head_sha = "a" * 40

    converted, errors = mod.adapt_test_verdict_to_current_vc_result(_sample_test_verdict())
    assert errors == []
    assert converted is not None

    baseline_item = {
        "ac": "AC1",
        "command_hash": command_hash,
        "failure_keys": [],
        "exit_code": 1,
        "category": "regression_gate",
        "classification": "expected_fail",
        "decision": "blocked",
        "raw_command": "pnpm test",
        "raw_stdout": "",
        "raw_stderr": "",
    }
    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": [baseline_item]}},
    }
    diff_summary = {"changed_paths": [".claude/skills/impl-review-loop/SKILL.md"], "head_sha": head_sha}

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=converted,
        diff_summary=diff_summary,
        allowed_paths=[".claude/skills/impl-review-loop/**"],
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False


def test_adapt_cli_subcommand_writes_converted_payload(tmp_path: Path):
    test_verdict_file = tmp_path / "test_verdict.json"
    test_verdict_file.write_text(json.dumps(_sample_test_verdict()), encoding="utf-8")
    adapt_out = tmp_path / "current_vc_result.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "adapt",
            "--test-verdict-file",
            str(test_verdict_file),
            "--adapt-out",
            str(adapt_out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    converted = json.loads(adapt_out.read_text(encoding="utf-8"))
    assert converted["schema"] == "baseline_vc_preflight/v1"
    assert converted["head_sha"] == "a" * 40


def test_step4_gate_cli_end_to_end_via_loop_state(tmp_path: Path):
    """End-to-end: adapt -> adjudicate -> LOOP_STATE.vc_adjudication ->
    `adjudicate_vc_result.py step4-gate` CLI, exit code 0 (invoke)."""
    command_hash = "sha256:" + "c" * 64
    body_sha256 = "sha256:" + "b" * 64
    head_sha = "a" * 40

    converted, errors = mod.adapt_test_verdict_to_current_vc_result(_sample_test_verdict())
    assert errors == []

    baseline_item = {
        "ac": "AC1",
        "command_hash": command_hash,
        "failure_keys": [],
        "exit_code": 1,
        "category": "regression_gate",
        "classification": "expected_fail",
        "decision": "blocked",
        "raw_command": "pnpm test",
        "raw_stdout": "",
        "raw_stderr": "",
    }
    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": [baseline_item]}},
    }
    diff_summary = {"changed_paths": [".claude/skills/impl-review-loop/SKILL.md"], "head_sha": head_sha}

    adjudication = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=converted,
        diff_summary=diff_summary,
        allowed_paths=[".claude/skills/impl-review-loop/**"],
    )
    assert adjudication["blocking"] is False

    loop_state: dict = {}
    mod.step4_persist_vc_adjudication(
        loop_state,
        head_sha=head_sha,
        contract_body_sha256=body_sha256,
        command_hashes=[command_hash],
        adjudication_result=adjudication,
    )
    assert loop_state["vc_adjudication"]

    loop_state_file = tmp_path / "loop_state.json"
    loop_state_file.write_text(json.dumps(loop_state), encoding="utf-8")
    hashes_file = tmp_path / "expected_hashes.json"
    hashes_file.write_text(json.dumps([command_hash]), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "step4-gate",
            "--loop-state-file",
            str(loop_state_file),
            "--expected-head-sha",
            head_sha,
            "--expected-contract-body-sha256",
            body_sha256,
            "--expected-command-hashes-file",
            str(hashes_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["invoke_pr_reviewer"] is True
    assert payload["reused"] is True


def test_step4_gate_cli_rerun_when_no_entry_yet(tmp_path: Path):
    loop_state_file = tmp_path / "loop_state.json"
    loop_state_file.write_text(json.dumps({}), encoding="utf-8")
    hashes_file = tmp_path / "expected_hashes.json"
    hashes_file.write_text(json.dumps(["sha256:" + "1" * 64]), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "step4-gate",
            "--loop-state-file",
            str(loop_state_file),
            "--expected-head-sha",
            "a" * 40,
            "--expected-contract-body-sha256",
            "sha256:" + "b" * 64,
            "--expected-command-hashes-file",
            str(hashes_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["invoke_pr_reviewer"] is False


def test_step4_gate_cli_malformed_loop_state_file_exit_2(tmp_path: Path):
    loop_state_file = tmp_path / "loop_state.json"
    loop_state_file.write_text("not json", encoding="utf-8")
    hashes_file = tmp_path / "expected_hashes.json"
    hashes_file.write_text(json.dumps(["sha256:" + "1" * 64]), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "step4-gate",
            "--loop-state-file",
            str(loop_state_file),
            "--expected-head-sha",
            "a" * 40,
            "--expected-contract-body-sha256",
            "sha256:" + "b" * 64,
            "--expected-command-hashes-file",
            str(hashes_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2


# --- Issue #2996: ensure_contract_snapshot envelope resolution (unit level) -------

_ENVELOPE_BODY = "sha256:" + "5" * 64


def _dry_run_envelope() -> dict:
    return {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "issue_number": 2996,
        "repo": "squne121/agent-platform",
        "status": "dry_run_would_post",
        "source": "materialized_go",
        "contract_snapshot_url": None,
        "body_sha256_at_check": _ENVELOPE_BODY,
        "contract_review_once_result": {
            "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
            "issue_number": 2996,
            "status": "go",
            "body_sha256": _ENVELOPE_BODY,
            "vc_preflight_classifications": [_payload_item("AC1")],
        },
    }


def test_resolve_envelope_builds_one_canonical_object_used_by_every_stage():
    resolved, errors = mod.resolve_step4_contract_snapshot(
        _dry_run_envelope(), producer_exit_code="20", expected_contract_body_sha256=_ENVELOPE_BODY
    )
    assert errors == []
    assert resolved["schema"] == "CONTRACT_REVIEW_RESULT_V1"
    assert resolved["status"] == "go"
    assert resolved["body_sha256"] == _ENVELOPE_BODY
    items, normalize_errors, schema = mod._normalize_list_payload(resolved)
    assert normalize_errors == [] and schema == "CONTRACT_REVIEW_RESULT_V1"
    assert [item["ac"] for item in items] == ["AC1"]
    integrity = mod._extract_source_integrity(
        contract_snapshot=resolved,
        current_vc_result=None,
        diff_summary=None,
        allowed_paths=None,
        normalized_allowed=[],
        baseline_schema=schema,
        current_items_count=0,
        baseline_items_count=len(items),
        changed_paths_present=False,
    )
    assert integrity["contract_body_sha256"] == _ENVELOPE_BODY


def test_resolve_envelope_fail_closed_returns_no_partial_snapshot():
    envelope = _dry_run_envelope()
    for exit_code in (None, "", "x", "0", "10", "40"):
        resolved, errors = mod.resolve_step4_contract_snapshot(envelope, producer_exit_code=exit_code)
        assert resolved is None
        assert errors, exit_code
    human = dict(envelope, status="human_judgment")
    resolved, errors = mod.resolve_step4_contract_snapshot(human, producer_exit_code="20")
    assert resolved is None
    assert errors == ["producer_status_not_handoff_eligible:human_judgment"]


def test_resolve_non_envelope_snapshot_is_unchanged_and_rejects_exit_code():
    legacy = {"schema": "CONTRACT_REVIEW_RESULT_V1", "status": "go", "body_sha256": _ENVELOPE_BODY}
    assert mod.resolve_step4_contract_snapshot(legacy) == (legacy, [])
    assert mod.resolve_step4_contract_snapshot(legacy, producer_exit_code="0") == (
        None,
        ["producer_exit_code_requires_envelope_snapshot"],
    )
    assert mod.resolve_step4_contract_snapshot(None) == (None, [])


def test_envelope_is_still_not_a_plain_adjudicate_input():
    """`adjudicate` / `step4-gate` stay envelope-unaware (Issue #2996 AC1)."""
    _, errors, _ = mod._normalize_list_payload(_dry_run_envelope())
    assert errors == ["unsupported_schema:CONTRACT_SNAPSHOT_ENSURE_RESULT_V1"]
