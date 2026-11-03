"""I-4-4C1 negative controls: a missing producer must fail closed, a hollow migration must not pass.

No network and no `gh`: each case runs the real router from a copied layout in a clean
`python -I` subprocess, or feeds the closure helpers a broken manifest / tmp tree.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "closure_i4_4c1_for_negative_controls", ROOT / "tests" / "stage_i4" / "test_i4_4c1_closure.py"
)
assert _SPEC is not None and _SPEC.loader is not None
closure = importlib.util.module_from_spec(_SPEC)
sys.modules["closure_i4_4c1_for_negative_controls"] = closure
_SPEC.loader.exec_module(closure)


def _layout(tmp: Path, *, with_producer: bool = True) -> Path:
    for rel in closure.LAYOUT_FILES:
        if rel == closure.PRODUCER and not with_producer:
            continue
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    return tmp


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(closure._manifest())


@pytest.fixture
def stage1a() -> dict[str, Any]:
    return json.loads(closure.STAGE1A.read_text(encoding="utf-8"))


def test_copied_layout_positive_control(tmp_path: Path) -> None:
    d = closure.router_disposition(_layout(tmp_path), closure._merged_evidence())
    assert d["disposition"] == "implementation_already_landed"


def test_missing_producer_fails_closed_and_never_lands(tmp_path: Path) -> None:
    d = closure.router_disposition(
        _layout(tmp_path, with_producer=False), closure._merged_evidence()
    )
    assert d == {
        "disposition": "reconciliation_required",
        "reason_codes": [closure.UNAVAILABLE],
        "candidate": None,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["freshness"].update(status="stale"),
        lambda e: e.update(contradictory=True),
        lambda e: e["target"].update(issue_number=9999),
        lambda e: e["candidates"][0]["main_ancestry"].update(reachable=False),
    ],
)
def test_stale_contradictory_or_unverified_evidence_never_reports_landed(
    tmp_path: Path, mutate: Any
) -> None:
    evidence = closure._merged_evidence()
    mutate(evidence)
    d = closure.router_disposition(_layout(tmp_path), evidence)
    assert d["disposition"] != "implementation_already_landed"


def test_drifted_moved_file_is_detected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    for rel in manifest["source_parity"]:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    assert closure.parity_problems(manifest, tmp_path) == []
    victim = tmp_path / closure.PRODUCER
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    assert any("moved sha mismatch" in p for p in closure.parity_problems(manifest, tmp_path))


def test_non_verbatim_producer_is_rejected(tmp_path: Path, manifest: dict[str, Any]) -> None:
    for rel in manifest["source_parity"]:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    manifest["source_parity"][closure.PRODUCER]["verbatim"] = False
    manifest["source_parity"][closure.PRODUCER]["intentional_diff"] = "edited"
    assert any("must be moved verbatim" in p for p in closure.parity_problems(manifest, tmp_path))


def test_stale_deferred_ref_is_detected(stage1a: dict[str, Any]) -> None:
    assert closure.deferred_record_problems(stage1a, ROOT) == []
    stale = copy.deepcopy(stage1a)
    stale["import_closure"]["deferred_script_refs"].append(
        {"script": "implementation_landed_evidence.py", "reason": "x", "owner_stage": "I-4-4C"}
    )
    assert any("still recorded" in p for p in closure.deferred_record_problems(stale, ROOT))


def test_dropping_the_c4_deferred_import_is_detected(stage1a: dict[str, Any]) -> None:
    broken = copy.deepcopy(stage1a)
    broken["import_closure"]["deferred_imports"] = [
        e
        for e in broken["import_closure"]["deferred_imports"]
        if e["module"] != "pr_head_replay_publish_exec"
    ]
    assert any(
        "deferred import was dropped" in p for p in closure.deferred_record_problems(broken, ROOT)
    )


def test_silent_deselection_is_detected(manifest: dict[str, Any]) -> None:
    assert closure.manifest_deferred_problems(manifest) == []
    manifest["deferred_source_tests"] = [{"id": "x"}]
    assert closure.manifest_deferred_problems(manifest)
    manifest["deferred_source_tests"] = []
    manifest["totals"]["skipped"] = 1
    assert closure.manifest_deferred_problems(manifest)


@pytest.mark.parametrize(
    "argv",
    [
        '["gh", "pr", "merge", "1"]',
        '["gh", "api", "-X", "POST", "repos/x/y/issues"]',
        '["gh", "issue", "edit", "1"]',
        '["git", "push", "--force"]',
    ],
)
def test_write_capable_argv_is_detected(argv: str) -> None:
    assert closure.gh_argv_problems(f"x = {argv}\n")
    assert closure.gh_argv_problems('x = ["gh", "pr", "view", "1"]\n') == []
