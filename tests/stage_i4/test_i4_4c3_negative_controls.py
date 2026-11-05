"""I-4-4C3 negative controls: a hollow, drifted or publishing materializer must not pass.

No network and no `gh`: closure helpers get broken manifests / tmp trees, and the materializer is
driven in-process with its executor present, missing or incomplete. Every output path is under
`tmp_path`; nothing outside it is created or deleted.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "closure_i4_4c3_for_negative_controls", ROOT / "tests" / "stage_i4" / "test_i4_4c3_closure.py"
)
assert _SPEC is not None and _SPEC.loader is not None
closure = importlib.util.module_from_spec(_SPEC)
sys.modules["closure_i4_4c3_for_negative_controls"] = closure
_SPEC.loader.exec_module(closure)

REQUEST_SCHEMA = "TEST_VERDICT_PUBLISH_INPUT_V1"
ARGS: dict[str, Any] = {
    "command_hash_map": {"AC1": "sha256:" + "a" * 64},
    "repo": "squne121/agent-platform",
    "issue_number": 85,
    "current_pr_number": 1,
    "current_head_sha": "a" * 40,
    "current_issue_body_sha256": "sha256:" + "b" * 64,
    "reviewed_head_sha": "a" * 40,
    "diff_head_sha": "a" * 40,
    "run_id": "1001",
    "run_url": "https://github.com/squne121/agent-platform/actions/runs/1001",
}


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(closure._manifest())


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


def _copied_layout(tmp: Path, executor_source: str | None) -> Any:
    """Copy only the materializer into tmp (so `parents[4]` is tmp) with a chosen executor."""
    dest = tmp / closure.MATERIALIZER
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / closure.MATERIALIZER, dest)
    if executor_source is not None:
        ex = tmp / closure.EXECUTOR
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(executor_source, encoding="utf-8")
    return _load(dest, f"materializer_copied_{abs(hash(str(tmp)))}")


def _copy_moved(manifest: dict[str, Any], tmp: Path) -> Path:
    for rel in manifest["source_parity"]:
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    return tmp


# --- manifest / parity -------------------------------------------------------------------------


def test_drifted_moved_file_is_detected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    _copy_moved(manifest, tmp_path)
    assert closure.parity_problems(manifest, tmp_path) == []
    victim = tmp_path / closure.MATERIALIZER
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    assert any("moved sha mismatch" in p for p in closure.parity_problems(manifest, tmp_path))


def test_undeclared_test_diff_is_rejected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    _copy_moved(manifest, tmp_path)
    test_rel = manifest["test_files"][0]
    info = manifest["source_parity"][test_rel]
    assert info["verbatim"] is False and info["intentional_diff"]
    info["intentional_diff"] = ""
    assert any("without intentional_diff" in p for p in closure.parity_problems(manifest, tmp_path))


def test_materializer_must_stay_verbatim(tmp_path: Path, manifest: dict[str, Any]) -> None:
    _copy_moved(manifest, tmp_path)
    info = manifest["source_parity"][closure.MATERIALIZER]
    info["verbatim"] = False
    info["intentional_diff"] = "claimed edit"
    assert any("verbatim move" in p for p in closure.parity_problems(manifest, tmp_path))


def test_missing_moved_file_is_detected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    _copy_moved(manifest, tmp_path)
    (tmp_path / closure.MATERIALIZER).unlink()
    assert any(p.startswith("missing:") for p in closure.parity_problems(manifest, tmp_path))


def test_deferred_record_is_rejected(manifest: dict[str, Any]) -> None:
    assert closure.deferred_problems(manifest) == []
    entry = {"id": "x::y", "owner_stage": "I-4-4D", "reason": "r", "rerun_condition": "c"}
    manifest["deferred_tests"].append(entry)
    assert any("0 deferred" in p for p in closure.deferred_problems(manifest))


def test_inconsistent_totals_are_detected(manifest: dict[str, Any]) -> None:
    manifest["totals"]["ran"] -= 1
    assert any("inconsistent" in p for p in closure.deferred_problems(manifest))
    manifest["totals"]["ran"] += 1
    manifest["totals"]["skipped"] = 1
    assert any("skipped" in p for p in closure.deferred_problems(manifest))


def test_skip_marker_in_a_moved_test_is_detected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    rel = manifest["test_files"][0]
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "import pytest\n\n@pytest.mark.skip(reason='x')\ndef test_a():\n    pytest.xfail('y')\n",
        encoding="utf-8",
    )
    problems = closure.skip_marker_problems(manifest, tmp_path)
    assert len(problems) == 2


# --- static boundary: shared executor reuse and no publish --------------------------------------


def test_redefined_shared_function_is_detected() -> None:
    source = (ROOT / closure.MATERIALIZER).read_text(encoding="utf-8")
    executor = (ROOT / closure.EXECUTOR).read_text(encoding="utf-8")
    assert closure.shared_executor_problems(source, executor) == []
    forged = source + "\n\ndef _verify_producer_run_and_job(*a, **k):\n    return ''\n"
    assert any(
        "redefined shared function" in p for p in closure.shared_executor_problems(forged, executor)
    )
    assert any("executor lacks" in p for p in closure.shared_executor_problems(source, "x = 1\n"))
    no_lookup = source.replace("_download_and_verify_artifact_archive", "_other")
    assert any(
        "does not look up" in p for p in closure.shared_executor_problems(no_lookup, executor)
    )


@pytest.mark.parametrize(
    "addition",
    [
        "import subprocess\n",
        "from subprocess import run\n",
        "import urllib.request\n",
        "X = 'test_verdict.publish'\n",
        "X = 'POST'\n",
        "X = 'push'\n",
        "X = 'gh'\n",
        "import os\nos.system('true')\n",
    ],
)
def test_publish_or_write_operation_is_detected(addition: str) -> None:
    source = (ROOT / closure.MATERIALIZER).read_text(encoding="utf-8")
    assert closure.no_publish_problems(source) == []
    assert closure.no_publish_problems(source + "\n" + addition)


def test_publish_word_in_a_docstring_is_not_a_false_positive() -> None:
    assert (
        closure.no_publish_problems('"""test_verdict.publish is not called here."""\nX = 1\n') == []
    )


# --- runtime fail-closed ----------------------------------------------------------------------


def test_missing_executor_in_a_copied_layout_is_blocked(tmp_path: Path) -> None:
    mod = _copied_layout(tmp_path, executor_source=None)
    status, input_bundle, private_bundle, errors = mod.materialize_test_verdict_artifact(
        publish_request={"schema": REQUEST_SCHEMA}, **ARGS
    )
    assert status == "blocked" and input_bundle is None and private_bundle is None
    assert len(errors) == 1
    assert errors[0].startswith("controlled_skill_mutation_exec_import_failed:")


def test_copied_layout_with_the_real_executor_is_the_positive_control(tmp_path: Path) -> None:
    """Same layout, executor present: the import succeeds, so the block comes from the request."""
    mod = _copied_layout(tmp_path, (ROOT / closure.EXECUTOR).read_text(encoding="utf-8"))
    status, input_bundle, _private, errors = mod.materialize_test_verdict_artifact(
        publish_request={"schema": REQUEST_SCHEMA}, **ARGS
    )
    assert status == "blocked" and input_bundle is None
    assert errors and not errors[0].startswith("controlled_skill_mutation_exec_")


def test_executor_without_the_validator_is_blocked(tmp_path: Path) -> None:
    mod = _copied_layout(tmp_path, executor_source="X = 1\n")
    status, input_bundle, _private, errors = mod.materialize_test_verdict_artifact(
        publish_request={"schema": REQUEST_SCHEMA}, **ARGS
    )
    assert status == "blocked" and input_bundle is None
    assert errors == ["controlled_skill_mutation_exec_missing_validator"]


def test_executor_without_readback_functions_is_blocked(tmp_path: Path) -> None:
    mod = _copied_layout(tmp_path, executor_source="def _find_gh_bin():\n    return 'gh', ''\n")
    record, errors = mod._live_readback_execution_record(
        {"schema": "x"}, "squne121/agent-platform", "a" * 40, "gh", {}
    )
    assert record is None
    assert errors == ["controlled_skill_mutation_exec_missing_live_readback_functions"]


def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("subprocess must not run for an input that fails before readback")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_receipt_less_request_is_blocked_before_any_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_subprocess(monkeypatch)
    mod = _load(ROOT / closure.MATERIALIZER, "materializer_receiptless_c3")
    status, input_bundle, private_bundle, errors = mod.materialize_test_verdict_artifact(
        publish_request={"schema": REQUEST_SCHEMA, "target_pr_number": 1}, **ARGS
    )
    assert status == "blocked" and input_bundle is None and private_bundle is None and errors


def test_hand_written_verdict_never_produces_a_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-attested TEST_VERDICT with no receipt: blocked, no bundle, stale outputs removed."""
    _no_subprocess(monkeypatch)
    mod = _load(ROOT / closure.MATERIALIZER, "materializer_handwritten_c3")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"schema": "TEST_VERDICT_MACHINE/v2", "result": "PASS"}), encoding="utf-8"
    )
    hashes = tmp_path / "hashes.json"
    hashes.write_text(json.dumps(ARGS["command_hash_map"]), encoding="utf-8")
    out_in, out_priv = tmp_path / "out" / "input.json", tmp_path / "out" / "private.json"
    out_in.parent.mkdir()
    out_in.write_text("stale", encoding="utf-8")
    out_priv.write_text("stale", encoding="utf-8")
    argv = [
        "--publish-request-file", str(request),
        "--command-hash-map-file", str(hashes),
        "--repo", ARGS["repo"],
        "--issue-number", "85",
        "--current-pr-number", "1",
        "--current-head-sha", ARGS["current_head_sha"],
        "--current-issue-body-sha256", ARGS["current_issue_body_sha256"],
        "--reviewed-head-sha", ARGS["reviewed_head_sha"],
        "--diff-head-sha", ARGS["diff_head_sha"],
        "--run-id", ARGS["run_id"],
        "--run-url", ARGS["run_url"],
        "--out-input-file", str(out_in),
        "--out-private-file", str(out_priv),
    ]  # fmt: skip
    assert mod.main(argv) == 1
    assert not out_in.exists() and not out_priv.exists()
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == [
        "hashes.json",
        "request.json",
    ]
