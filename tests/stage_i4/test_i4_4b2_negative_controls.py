"""I-4-4B2 negative controls: the closure detectors must fail loudly on tampering.

Pure data/tmp-path checks (no network, no `gh`): each case feeds the closure helpers a broken
manifest or tests directory and asserts a problem is reported, so a hollow migration (silent
deletion, drifted file, restored-but-still-deferred test, B1 overlap) cannot pass unnoticed.
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
    "closure_i4_4b2_for_negative_controls", ROOT / "tests" / "stage_i4" / "test_i4_4b2_closure.py"
)
assert _SPEC is not None and _SPEC.loader is not None
closure = importlib.util.module_from_spec(_SPEC)
sys.modules["closure_i4_4b2_for_negative_controls"] = closure
_SPEC.loader.exec_module(closure)


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(closure._manifest())


@pytest.fixture
def b1() -> dict[str, Any]:
    return json.loads(closure.B1_MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture
def layout(tmp_path: Path, manifest: dict[str, Any]) -> Path:
    for rel in manifest["test_files"]:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    return tmp_path


def test_positive_control(layout: Path, manifest: dict[str, Any], b1: dict[str, Any]) -> None:
    assert closure.parity_problems(manifest, layout) == []
    assert closure.deferred_problems(manifest, b1, layout / closure.TESTS) == []


def test_drifted_moved_file_is_detected(layout: Path, manifest: dict[str, Any]) -> None:
    victim = layout / manifest["test_files"][0]
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    assert any("moved sha mismatch" in p for p in closure.parity_problems(manifest, layout))


def test_missing_moved_file_is_detected(layout: Path, manifest: dict[str, Any]) -> None:
    (layout / manifest["test_files"][0]).unlink()
    assert any(p.startswith("missing:") for p in closure.parity_problems(manifest, layout))


def test_non_verbatim_without_intentional_diff_is_detected(
    layout: Path, manifest: dict[str, Any]
) -> None:
    rel = next(r for r, i in manifest["source_parity"].items() if not i["verbatim"])
    manifest["source_parity"][rel].pop("intentional_diff")
    assert any("without intentional_diff" in p for p in closure.parity_problems(manifest, layout))


def test_false_verbatim_claim_is_detected(layout: Path, manifest: dict[str, Any]) -> None:
    rel = next(r for r, i in manifest["source_parity"].items() if not i["verbatim"])
    manifest["source_parity"][rel]["verbatim"] = True
    assert any("verbatim but sha differs" in p for p in closure.parity_problems(manifest, layout))


def test_restored_test_that_is_still_deferred_is_detected(
    layout: Path, manifest: dict[str, Any], b1: dict[str, Any]
) -> None:
    entry = manifest["deferred_source_tests"][0]
    rel_file, name = entry["id"].split("::")
    path = layout / closure.TESTS / rel_file
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n\ndef {name}():\n    pass\n", encoding="utf-8"
    )
    problems = closure.deferred_problems(manifest, b1, layout / closure.TESTS)
    assert any("still defined" in p for p in problems)


def test_deferred_entry_without_owner_or_rerun_condition_is_detected(
    layout: Path, manifest: dict[str, Any], b1: dict[str, Any]
) -> None:
    manifest["deferred_source_tests"][0]["owner_stage"] = ""
    manifest["deferred_source_tests"][1]["rerun_condition"] = ""
    problems = closure.deferred_problems(manifest, b1, layout / closure.TESTS)
    assert sum("incomplete deferred entry" in p for p in problems) == 2


def test_unknown_owner_is_detected(
    layout: Path, manifest: dict[str, Any], b1: dict[str, Any]
) -> None:
    manifest["deferred_source_tests"][0]["owner_stage"] = "nobody"
    assert any(
        "unknown owner" in p
        for p in closure.deferred_problems(manifest, b1, layout / closure.TESTS)
    )


def test_overlap_with_b1_deferred_is_detected(
    layout: Path, manifest: dict[str, Any], b1: dict[str, Any]
) -> None:
    manifest["deferred_source_tests"][0]["id"] = b1["deferred_source_tests"][0]["id"]
    assert any(
        "duplicate" in p for p in closure.deferred_problems(manifest, b1, layout / closure.TESTS)
    )


def test_silently_dropped_deferred_entry_breaks_the_totals(
    layout: Path, manifest: dict[str, Any], b1: dict[str, Any]
) -> None:
    manifest["deferred_source_tests"].pop()
    problems = closure.deferred_problems(manifest, b1, layout / closure.TESTS)
    assert any("!= totals" in p or "ran + removed" in p for p in problems)


def test_skip_marker_pattern_catches_skip_and_xfail() -> None:
    for text in (
        "@pytest.mark.skip",
        "pytest.mark.xfail(reason='x')",
        "pytest.skip('x')",
        "pytest.xfail()",
    ):
        assert closure.SKIP_MARKERS.search(text), text
    assert not closure.SKIP_MARKERS.search("def test_skipped_fact_is_not_pass(): ...")
