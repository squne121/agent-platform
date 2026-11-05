"""I-4-4C2 negative controls: a hollow, drifted or silently-weakened migration must not pass.

No network and no `gh`: closure helpers get broken manifests / tmp trees, and the capsule's
blocked-case path is driven in-process with its triage dependency present or missing.
"""

from __future__ import annotations

import copy
import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "closure_i4_4c2_for_negative_controls", ROOT / "tests" / "stage_i4" / "test_i4_4c2_closure.py"
)
assert _SPEC is not None and _SPEC.loader is not None
closure = importlib.util.module_from_spec(_SPEC)
sys.modules["closure_i4_4c2_for_negative_controls"] = closure
_SPEC.loader.exec_module(closure)


RUNNABLE_FILE = (
    ".claude/skills/impl-review-loop/tests/test_build_intake_capsule_advisory_routing.py"
)
RUNNABLE_FUNCTION = "test_missing_go_routes_to_proceed_to_step_1"


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(closure._manifest())


def _copy_moved(manifest: dict[str, Any], tmp: Path) -> Path:
    for rel in manifest["source_parity"]:
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    return tmp


def _capsule() -> Any:
    spec = importlib.util.spec_from_file_location("capsule_for_c2_negative", ROOT / closure.CAPSULE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["capsule_for_c2_negative"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("capsule_for_c2_negative", None)
    return mod


def test_drifted_moved_file_is_detected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    _copy_moved(manifest, tmp_path)
    assert closure.parity_problems(manifest, tmp_path) == []
    victim = tmp_path / closure.TRIAGE
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    assert any("moved sha mismatch" in p for p in closure.parity_problems(manifest, tmp_path))


def test_undeclared_diff_is_rejected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    _copy_moved(manifest, tmp_path)
    info = manifest["source_parity"][closure.CAPSULE]
    assert info["verbatim"] is False and info["intentional_diff"]
    info["intentional_diff"] = ""
    assert any("without intentional_diff" in p for p in closure.parity_problems(manifest, tmp_path))
    verbatim = manifest["source_parity"][closure.TRIAGE]
    verbatim["source_sha256"] = "0" * 64
    assert any("verbatim but sha differs" in p for p in closure.parity_problems(manifest, tmp_path))


def test_missing_moved_file_is_detected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    _copy_moved(manifest, tmp_path)
    (tmp_path / closure.VERIFIER).unlink()
    assert any(p.startswith("missing:") for p in closure.parity_problems(manifest, tmp_path))


def test_deferred_records_are_complete_positive_control(manifest: dict[str, Any]) -> None:
    assert closure.deferred_problems(manifest, ROOT) == []


def test_silently_dropped_deferred_entry_is_detected(manifest: dict[str, Any]) -> None:
    manifest["deferred_source_tests"].pop()
    assert any("differ" in p for p in closure.deferred_problems(manifest, ROOT))


def test_dropping_the_live_canary_entry_is_detected(manifest: dict[str, Any]) -> None:
    keep = [e for e in manifest["deferred_tests"] if e["kind"] != "live_github_canary"]
    manifest["deferred_tests"] = keep
    manifest["deferred_source_tests"] = copy.deepcopy(keep)
    assert any("live GitHub canary" in p for p in closure.deferred_problems(manifest, ROOT))


@pytest.mark.parametrize("key", ["reason", "owner_stage", "rerun_condition", "function"])
def test_incomplete_deferred_entry_is_detected(manifest: dict[str, Any], key: str) -> None:
    manifest["deferred_tests"][0][key] = ""
    manifest["deferred_source_tests"][0][key] = ""
    assert closure.deferred_problems(manifest, ROOT)


def test_wrong_deferred_owner_is_detected(manifest: dict[str, Any]) -> None:
    manifest["deferred_tests"][0]["owner_stage"] = "I-4-4C3"
    manifest["deferred_source_tests"][0]["owner_stage"] = "I-4-4C3"
    assert any("unexpected owner" in p for p in closure.deferred_problems(manifest, ROOT))


def test_deferred_document_that_now_exists_is_stale(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    doc = next(e for e in manifest["deferred_tests"] if e["kind"] == "unmigrated_document")
    present = tmp_path / doc["requires_missing_files"][0]
    present.parent.mkdir(parents=True, exist_ok=True)
    present.write_text("# migrated by I-4-4D\n", encoding="utf-8")
    assert any("stale deferred entry" in p for p in closure.deferred_problems(manifest, tmp_path))


def test_count_tampering_is_detected(manifest: dict[str, Any]) -> None:
    manifest["totals"]["ran"] += 1
    assert closure.deferred_problems(manifest, ROOT)


def test_skip_outside_a_deferred_function_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    rel = manifest["test_files"][0]
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "import pytest\n\n\ndef test_a():\n    pytest.skip('x')\n\n\n"
        "@pytest.mark.xfail\ndef test_b():\n    assert False\n",
        encoding="utf-8",
    )
    manifest["test_files"] = [rel]
    problems = closure.skip_marker_problems(manifest, tmp_path)
    assert len(problems) == 2
    # the same skip inside a manifest-deferred function is the only accepted shape
    manifest["deferred_tests"] = [
        {"file": rel, "function": "test_a"},
        {"file": rel, "function": "test_b"},
    ]
    assert closure.skip_marker_problems(manifest, tmp_path) == []


@pytest.mark.parametrize(
    "argv",
    [
        '["gh", "pr", "merge", "1"]',
        '["gh", "pr", "comment", "1"]',
        '["gh", "api", "-X", "POST", "repos/x/y/issues"]',
        '["gh", "issue", "edit", "1"]',
        '["git", "push", "--force"]',
        '["git", "commit", "-m", "x"]',
    ],
)
def test_write_capable_argv_is_detected(argv: str) -> None:
    assert closure.argv_problems(f"x = {argv}\n")


@pytest.mark.parametrize(
    "argv",
    ['["gh", "pr", "view", "1"]', '["gh", "issue", "list"]', '["git", "rev-parse", "HEAD"]'],
)
def test_read_argv_is_accepted(argv: str) -> None:
    assert closure.argv_problems(f"x = {argv}\n") == []


def test_blocked_snapshot_is_classified_by_the_migrated_triage() -> None:
    mod = _capsule()
    out = mod._normalize_contract_snapshot_from_ensure({"status": "blocked_needs_refinement"})
    assert out["normalized_status"] == "missing_go"
    triage = out["contract_blocker_triage"]
    assert triage["schema"] == "CONTRACT_BLOCKER_TRIAGE_V1"
    assert triage["status"] != "not_run"
    assert triage["step1_allowed"] is False


def test_go_snapshot_never_needs_triage_and_stays_not_run() -> None:
    out = _capsule()._normalize_contract_snapshot_from_ensure({"status": "ok"})
    assert out["normalized_status"] == "go"
    assert out["contract_blocker_triage"]["status"] == "not_run"


def test_missing_triage_makes_the_blocked_case_fail_closed(tmp_path: Path) -> None:
    mod = _capsule()
    mod._TRIAGE_PATH = tmp_path / "triage_contract_blockers.py"
    with pytest.raises(FileNotFoundError):
        mod._normalize_contract_snapshot_from_ensure({"status": "blocked_needs_refinement"})
    # a "go" snapshot does not touch triage, so it is unaffected by the missing file
    assert (
        mod._normalize_contract_snapshot_from_ensure({"status": "ok"})["normalized_status"] == "go"
    )


def test_functional_default_repo_is_not_the_legacy_slug() -> None:
    assert _capsule()._DEFAULT_REPO == "squne121/agent-platform"
    verifier = (ROOT / closure.VERIFIER).read_text(encoding="utf-8")
    assert 'REPO = "squne121/agent-platform"' in verifier


def test_ci_lane_guard_removal_is_detected() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert closure.ci_lane_problems(text) == []
    assert closure.ci_lane_problems(text.replace('7 deselected" runner.out', "x")) != []
    assert closure.ci_lane_problems("jobs: {}") != []


def test_deferred_entry_pointing_at_a_runnable_test_is_detected(manifest: dict[str, Any]) -> None:
    missing = copy.deepcopy(manifest)
    missing["deferred_tests"][0]["function"] = "test_does_not_exist"
    assert any("deferred function not found" in p for p in closure.deferred_problems(missing, ROOT))
    # a runnable test that reads no document cannot be passed off as a document-dependent deferral
    runnable = copy.deepcopy(manifest)
    entry = runnable["deferred_tests"][0]
    entry["file"] = RUNNABLE_FILE
    entry["function"] = RUNNABLE_FUNCTION
    assert any("does not read a document" in p for p in closure.deferred_problems(runnable, ROOT))
