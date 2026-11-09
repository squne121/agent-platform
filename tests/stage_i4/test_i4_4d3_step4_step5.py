"""I-4-4D3: Step 4 / Step 5 documents + restored B1/B2 deferred tests (#93).

Document wording tests, the restored source tests running on the real documents, and tmp_path
only. Nothing here starts a SubAgent, calls live GitHub, runs the push-capable replay executor,
mutates a Git ref, publishes the Skill entrypoint or wires Final Gate / `LOOP_HANDOFF_RESULT_V1`.

Test groups are selected by the Issue's Verification Commands with `-k test_ac<N>_`.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d3-step4-step5-manifest.json"
B1_MANIFEST = DEV / "i4-4b1-vc-adjudication-manifest.json"
B2_MANIFEST = DEV / "i4-4b2-vc-step4-tests-manifest.json"
D1_MANIFEST = DEV / "i4-4d1-preparation-step1-manifest.json"
D2_MANIFEST = DEV / "i4-4d2-step2-verification-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
IRL = ".claude/skills/impl-review-loop"
TESTS = f"{IRL}/tests"
D3_JOB = "i4-4d3-step4-step5"
LANES = {
    "i4-4b1-vc-adjudication": "docs/dev/i4-4b1-vc-adjudication-manifest.json",
    "i4-4b2-vc-step4-tests": "docs/dev/i4-4b2-vc-step4-tests-manifest.json",
    "i4-4d1-preparation-step1": "docs/dev/i4-4d1-preparation-step1-manifest.json",
    "i4-4d2-step2-verification": "docs/dev/i4-4d2-step2-verification-manifest.json",
    D3_JOB: "docs/dev/i4-4d3-step4-step5-manifest.json",
}
# Pins fixed by the Issue contract (#93): the source blobs of the three documents.
ISSUE_SOURCE_BLOBS = {
    f"{IRL}/steps/step-4-pr-review.md": "7e293f06957ff2856644d8e3d7841ed69bcb8144",
    f"{IRL}/steps/step-5-feedback-and-termination.md": "c7c486bb9d6f1e687b40d084791e570933ede7b6",
    f"{IRL}/steps/step-5-mergeability-handling.md": "a60c45c1b1975b143dcb4e511134b2afda921d43",
}
ORACLE_ID = "test_independent_vc_terminal_gate_regression.py::test_terminal_gate_resolves_semantic_ambiguity"
# Historical measurements; they are preserved, never rewritten.
B1_HISTORY = {"ran": 118, "deferred_source_tests": 5}
B2_PRE_D3 = {
    "ran": 366,
    "source_collected_before_removal": 368,
    "removed_to_deferred_source_tests_cases": 2,
    "removed_functions": 2,
}
B2_PRE_D2 = {
    "source_collected_before_removal": 368,
    "ran": 298,
    "removed_to_deferred_source_tests_cases": 70,
    "removed_functions": 22,
}


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


d2 = _load("tests/stage_i4/test_i4_4d2_step2_verification.py", "d2_helpers_for_i4_4d3")
b2_closure = _load("tests/stage_i4/test_i4_4b2_closure.py", "b2_closure_for_i4_4d3")
document_problems = d2.document_problems
function_source = d2.function_source
job_runs = d2.job_runs


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_manifest())


@pytest.fixture
def b1() -> dict[str, Any]:
    return copy.deepcopy(_json(B1_MANIFEST))


@pytest.fixture
def b2() -> dict[str, Any]:
    return copy.deepcopy(_json(B2_MANIFEST))


def _restored(m: dict[str, Any]) -> list[dict[str, Any]]:
    return [*m["restored_b1_tests"], *m["restored_b2_tests"]]


def restored_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Every restored function exists exactly once with its recorded source body hash."""
    problems: list[str] = []
    restored = _restored(m)
    if len(restored) != m["totals"]["restored_functions"]:
        problems.append("restored function count != totals")
    if sum(e["cases"] for e in restored) != m["totals"]["restored_cases"]:
        problems.append("restored case count != totals")
    for e in restored:
        path = root / TESTS / e["file"]
        src = function_source(path, e["function"]) if path.is_file() else None
        if src is None:
            problems.append(f"restored function missing: {e['function']}")
        elif hashlib.sha256(src.encode("utf-8")).hexdigest() != e["body_sha256"]:
            problems.append(f"restored function body differs from the source: {e['function']}")
        elif re.search(
            r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|pytest\.xfail\(", src
        ):  # never green by skipping
            problems.append(f"restored function is skipped or xfailed: {e['function']}")
    return problems


def d3_sync_problems(
    m: dict[str, Any], b1_manifest: dict[str, Any], b2_manifest: dict[str, Any], ci: str
) -> list[str]:
    """B1/B2 current counts, deferred records, history and CI pins against the D3 record."""
    problems: list[str] = []
    b1_deferred = {e["id"]: e for e in b1_manifest["deferred_source_tests"]}
    for e in m["restored_b1_tests"]:
        if f"{e['file']}::{e['function']}" in b1_deferred:
            problems.append(f"restored test is still deferred in B1: {e['function']}")
    if set(b1_deferred) != {e["id"] for e in m["remaining_b1_deferred"]}:
        problems.append("B1 deferred set differs from the D3 remaining_b1_deferred record")
    for e in m["remaining_b1_deferred"]:
        if not all(e.get(k) for k in ("id", "reason", "owner_stage", "rerun_condition")):
            problems.append(f"remaining B1 deferred entry is incomplete: {e.get('id')}")
        if e["id"] == ORACLE_ID and not e["owner_stage"].startswith("I-4-4C"):
            problems.append("the oracle test lost its I-4-4C owner")
    cur1 = m["b1_counts"]["current"]
    if (b1_manifest["totals"]["ran"], len(b1_manifest["deferred_source_tests"])) != (
        cur1["ran"],
        cur1["deferred_source_tests"],
    ):
        problems.append("B1 totals differ from the current counts recorded by D3")
    if sum(b1_manifest["expected_ran"].values()) != b1_manifest["totals"]["ran"]:
        problems.append("B1 expected_ran does not sum to totals.ran")
    for hist in (m["b1_counts"]["historical_pre_d3"], b1_manifest.get("historical_pre_d3", {})):
        if any(hist.get(k) != v for k, v in B1_HISTORY.items()):
            problems.append("the historical B1 measurement (118 ran / 5 deferred) was lost")
    for e in m["restored_b2_tests"]:
        if f"{e['file']}::{e['function']}" in {
            x["id"] for x in b2_manifest["deferred_source_tests"]
        }:
            problems.append(f"restored test is still deferred in B2: {e['function']}")
    if b2_manifest["deferred_source_tests"] or b2_manifest["deferred_dependencies"]:
        problems.append("B2 still records deferred source tests or dependencies")
    cur2 = m["b2_counts"]["current"]
    t2 = b2_manifest["totals"]
    if (
        t2["ran"],
        t2["removed_functions"],
        t2["removed_to_deferred_source_tests_cases"],
        t2["source_collected_before_removal"],
    ) != (
        cur2["ran"],
        cur2["deferred_functions"],
        cur2["deferred_cases"],
        cur2["source_collected"],
    ):
        problems.append("B2 totals differ from the current counts recorded by D3")
    if sum(b2_manifest["expected_ran"].values()) != t2["ran"]:
        problems.append("B2 expected_ran does not sum to totals.ran")
    pre_d3 = b2_manifest.get("historical_pre_d3", {}).get("totals", {})
    if any(pre_d3.get(k) != v for k, v in B2_PRE_D3.items()) or any(
        m["b2_counts"]["historical_pre_d3"].get(k) != v for k, v in B2_PRE_D3.items()
    ):
        problems.append("the historical B2 measurement before D3 (366 ran / 2 deferred) was lost")
    pre_d2 = b2_manifest.get("historical_pre_d2", {}).get("totals", {})
    if any(pre_d2.get(k) != v for k, v in B2_PRE_D2.items()):
        problems.append("the historical B2 measurement (368/298/70/22) was lost or rewritten")
    problems.extend(b2_closure.deferred_problems(b2_manifest, b1_manifest, ROOT / b2_closure.TESTS))
    pins = {
        "i4-4b1-vc-adjudication": b1_manifest["totals"]["ran"],
        "i4-4b2-vc-step4-tests": t2["ran"],
        "i4-4d1-preparation-step1": _json(D1_MANIFEST)["totals"]["ran"],
        "i4-4d2-step2-verification": _json(D2_MANIFEST)["totals"]["ran"],
        D3_JOB: m["totals"]["ran"],
    }
    for job, ran in pins.items():
        runs = job_runs(ci, job)
        if runs is None:
            problems.append(f"CI lane missing: {job}")
            continue
        if f"run_stage1b1_tests.py {LANES[job]}" not in runs:
            problems.append(f"CI lane does not run its manifest: {job}")
        if f"0 deferred with reasons, {ran} ran, 0 skipped" not in runs:
            problems.append(f"CI lane does not pin the manifest ran count: {job}")
        if (
            "(skipped|xfailed|xpassed|deselected)" not in runs
            and "[0-9]+ (skipped|xfailed" not in runs
        ):
            problems.append(f"CI lane does not reject skip/xfail/deselect: {job}")
    return problems


def stale_absence_problems(root: Path) -> list[str]:
    """D1/D2 must no longer assert the absence of documents D3 migrated."""
    problems: list[str] = []
    for rel in (
        "tests/stage_i4/test_i4_4d1_preparation_step1.py",
        "tests/stage_i4/test_i4_4d2_step2_verification.py",
    ):
        for number, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(
                r"assert not .*steps\"\s*/\s*\"step-(4-pr-review|5-feedback-and-termination|"
                r"5-mergeability-handling)\.md\"",
                line,
            ):
                problems.append(f"stale absence assertion: {rel}:{number}")
    return problems


# --- AC1: source parity, pins and boundaries ---------------------------------------------


def test_ac1_documents_reproduce_the_pinned_source(manifest: dict[str, Any]) -> None:
    assert document_problems(manifest, ROOT) == []
    assert list(manifest["documents"]) == list(ISSUE_SOURCE_BLOBS)


def test_ac1_source_pins_are_the_ones_fixed_by_the_issue(manifest: dict[str, Any]) -> None:
    # the legacy repository identity is not hard-coded here: it must equal the pin D2 recorded
    assert manifest["source"] == _json(D2_MANIFEST)["source"]
    assert manifest["source"]["tag"] == "pre-split-agent-platform-20261020"
    assert manifest["source"]["commit"] == "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
    assert manifest["source"]["read_only"] is True
    for rel, blob in ISSUE_SOURCE_BLOBS.items():
        assert manifest["documents"][rel]["source_blob_sha1"] == blob, rel


def test_ac1_documents_carry_a_provenance_note_and_do_not_overclaim(
    manifest: dict[str, Any],
) -> None:
    for rel, rec in manifest["documents"].items():
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# "), rel
        assert lines[1] == "" and lines[2].startswith("> **移管状況 (I-4-4D3)**"), rel
        assert "i4-4d3-step4-step5-manifest.json" in lines[2], rel
        assert "起動できるものとして扱ってはならない" in lines[2], rel
        assert "本番稼働している" in lines[2], rel
        assert len(rec["intentional_diff"]) == 1, rel
    for key, value in manifest["evidence_boundary"].items():
        if key != "kind":
            assert value is False, key


def test_ac1_references_are_classified_with_an_owner_and_really_absent(
    manifest: dict[str, Any],
) -> None:
    refs = manifest["unmigrated_references"]
    assert refs["later_i_4_4d_slices"] and refs["legacy_repository_specific"]
    text = "\n".join(f"{e['reference']} {e['owner']}" for e in refs["later_i_4_4d_slices"])
    for needed in (
        "SKILL.md",
        "pr-reviewer",
        "test-runner",
        "implementation-worker",
        "implement-issue",
        "open-pr",
        "pr-review-judge",
        "wait_ci_checks",
        "body_only_repair_plan",
        "update_branch.py",
        "publish_termination_report.py",
    ):
        assert needed in text, needed
    assert all(e["owner"] and e["reference"] for e in refs["later_i_4_4d_slices"])
    assert "legacy repository slug" in "\n".join(refs["legacy_repository_specific"])
    # nothing the documents link to is faked: the unmigrated targets genuinely do not exist
    irl = ROOT / IRL
    for absent in (
        irl / "SKILL.md",
        ROOT / ".claude" / "agents" / "pr-reviewer.md",
        ROOT / ".claude" / "agents" / "test-runner.md",
        ROOT / ".claude" / "agents" / "implementation-worker.md",
        ROOT / ".claude" / "skills" / "implement-issue",
        ROOT / ".claude" / "skills" / "open-pr",
        ROOT / ".claude" / "skills" / "pr-review-judge" / "SKILL.md",
        irl / "scripts" / "wait_ci_checks.py",
        irl / "scripts" / "body_only_repair_plan.py",
    ):
        assert not absent.exists(), absent
    for rel in manifest["migrated_dependencies"]:
        assert (ROOT / rel).is_file(), rel


# --- AC2: the six restored tests run for real --------------------------------------------


def test_ac2_restored_functions_keep_their_source_bodies(manifest: dict[str, Any]) -> None:
    assert restored_problems(manifest, ROOT) == []
    assert len(manifest["restored_b1_tests"]) == 4
    assert len(manifest["restored_b2_tests"]) == 2
    names = [e["function"] for e in _restored(manifest)]
    assert len(names) == 6 and len(set(names)) == 6
    assert manifest["totals"]["restored_cases"] == 6


def test_ac2_restored_functions_collect_and_pass_on_the_real_documents(
    manifest: dict[str, Any],
) -> None:
    ids = [f"{TESTS}/{e['file']}::{e['function']}" for e in _restored(manifest)]
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rsxX"]
    collected = subprocess.run(
        [*cmd, "--collect-only", "-q", *ids], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    per_function: dict[str, int] = {}
    for line in collected.stdout.splitlines():
        if "::" in line:
            name = line.split("::")[1].split("[")[0]
            per_function[name] = per_function.get(name, 0) + 1
    assert per_function == {e["function"]: e["cases"] for e in _restored(manifest)}
    r = subprocess.run([*cmd, "-q", *ids], cwd=ROOT, capture_output=True, text=True, check=False)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "6 passed" in out, out
    assert not any(w in out for w in ("skipped", "xfailed", "xpassed", "deselected")), out


def test_ac2_oracle_dependent_test_stays_deferred_with_its_owner(
    manifest: dict[str, Any], b1: dict[str, Any]
) -> None:
    remaining = manifest["remaining_b1_deferred"]
    assert [e["id"] for e in remaining] == [ORACLE_ID]
    assert remaining[0]["owner_stage"].startswith("I-4-4C")
    assert [e["id"] for e in b1["deferred_source_tests"]] == [ORACLE_ID]
    rel_file, name = ORACLE_ID.split("::")
    text = (ROOT / TESTS / rel_file).read_text(encoding="utf-8")
    assert not re.search(rf"^def {name}\b", text, re.MULTILINE)
    # the push-capable replay executor is neither imported nor run by any restored test
    for e in _restored(manifest):
        body = function_source(ROOT / TESTS / e["file"], e["function"]) or ""
        assert "pr_head_replay_publish_exec" not in body, e["function"]
    for key in ("merge_tree_oracle_real_evidence", "git_ref_mutation"):
        assert manifest["evidence_boundary"][key] is False


# --- AC3: current counts, deferred records and CI pins -----------------------------------


def test_ac3_b1_b2_sync_matches_the_manifests_history_and_ci(
    manifest: dict[str, Any], b1: dict[str, Any], b2: dict[str, Any]
) -> None:
    assert d3_sync_problems(manifest, b1, b2, CI.read_text(encoding="utf-8")) == []
    assert manifest["b1_counts"]["current"] == {"ran": 122, "deferred_source_tests": 1}
    assert manifest["b2_counts"]["current"]["ran"] == 368
    assert manifest["b2_counts"]["current"]["deferred_functions"] == 0
    assert b2["deferred_source_tests"] == []


def _collected(manifest_path: Path) -> int:
    files = _json(manifest_path)["test_files"]
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    match = re.search(r"(\d+) tests? collected", r.stdout)
    assert match, r.stdout
    return int(match.group(1))


def test_ac3_current_counts_are_measured_not_assumed(manifest: dict[str, Any]) -> None:
    assert _collected(B1_MANIFEST) == manifest["b1_counts"]["current"]["ran"]
    assert _collected(B2_MANIFEST) == manifest["b2_counts"]["current"]["ran"]
    # the previous measurements plus what D3 restored are the current ones
    assert manifest["b1_counts"]["historical_pre_d3"]["ran"] + 4 == 122
    assert manifest["b2_counts"]["historical_pre_d3"]["ran"] + 2 == 368


def test_ac3_empty_b2_deferred_list_is_verified_consistently(
    b1: dict[str, Any], b2: dict[str, Any]
) -> None:
    assert b2["deferred_source_tests"] == []
    t = b2["totals"]
    assert (t["removed_functions"], t["removed_to_deferred_source_tests_cases"]) == (0, 0)
    assert t["ran"] == t["source_collected_before_removal"] == 368
    assert b2_closure.deferred_problems(b2, b1, ROOT / b2_closure.TESTS) == []


def test_ac3_d3_lane_runs_its_own_manifest_and_all_prior_lanes_stay_pinned(
    manifest: dict[str, Any],
) -> None:
    ci = CI.read_text(encoding="utf-8")
    for job, manifest_rel in LANES.items():
        runs = job_runs(ci, job)
        assert runs is not None, job
        assert f"run_stage1b1_tests.py {manifest_rel}" in runs, job
    assert manifest["totals"]["ran"] == _collected(MANIFEST)


# --- AC4: negative controls --------------------------------------------------------------


def test_ac4_missing_or_tampered_document_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    assert any("missing document" in p for p in document_problems(manifest, tmp_path))
    for rel in manifest["documents"]:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / rel).read_bytes())
    assert document_problems(manifest, tmp_path) == []
    first = next(iter(manifest["documents"]))
    (tmp_path / first).write_text(
        (tmp_path / first).read_text(encoding="utf-8") + "\nweakened\n", encoding="utf-8"
    )
    assert any("moved_sha256 mismatch" in p for p in document_problems(manifest, tmp_path))


def test_ac4_wrong_source_pin_or_hollow_diff_is_detected(manifest: dict[str, Any]) -> None:
    first = next(iter(manifest["documents"]))
    wrong_sha = copy.deepcopy(manifest)
    wrong_sha["documents"][first]["source_blob_sha1"] = "0" * 40
    assert any("source blob SHA" in p for p in document_problems(wrong_sha, ROOT))
    wrong_commit = copy.deepcopy(manifest)
    wrong_commit["source"]["commit"] = "0" * 40
    assert any("source commit pin" in p for p in document_problems(wrong_commit, ROOT))
    hollow = copy.deepcopy(manifest)
    hollow["documents"][first]["intentional_diff"] = []
    assert any("intentional_diff not recorded" in p for p in document_problems(hollow, ROOT))
    for rel in ISSUE_SOURCE_BLOBS:
        broken = copy.deepcopy(manifest)
        broken["documents"][rel]["source_blob_sha1"] = "1" * 40
        assert any("source blob SHA" in p and rel in p for p in document_problems(broken, ROOT)), (
            rel
        )


def test_ac4_missing_or_weakened_restored_function_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    for e in _restored(manifest):
        dest = tmp_path / TESTS / e["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / TESTS / e["file"]).read_bytes())
    assert restored_problems(manifest, tmp_path) == []
    for e in _restored(manifest):
        dest = tmp_path / TESTS / e["file"]
        original = dest.read_text(encoding="utf-8")
        src = function_source(dest, e["function"])
        assert src is not None
        # an assertion silently weakened inside a restored function
        dest.write_text(
            original.replace(src, src.replace("assert ", "assert True or ", 1)), "utf-8"
        )
        assert any("body differs" in p for p in restored_problems(manifest, tmp_path)), e
        # a skip marker added to a restored function
        skipped = original.replace(src, "@pytest.mark.skip\n" + src)
        dest.write_text(skipped, "utf-8")
        assert restored_problems(manifest, tmp_path) != [], e
        # a restored function that was never restored (renamed away)
        dest.write_text(
            original.replace(f"def {e['function']}(", f"def x_{e['function']}(", 1), "utf-8"
        )
        assert any("restored function missing" in p for p in restored_problems(manifest, tmp_path))
        dest.write_text(original, encoding="utf-8")


def test_ac4_false_deferred_and_unrestored_records_are_detected(
    manifest: dict[str, Any], b1: dict[str, Any], b2: dict[str, Any]
) -> None:
    ci = CI.read_text(encoding="utf-8")
    template = copy.deepcopy(b1["deferred_source_tests"][0])
    for e in manifest["restored_b1_tests"]:
        broken = copy.deepcopy(b1)
        entry = copy.deepcopy(template)
        entry["id"] = f"{e['file']}::{e['function']}"
        broken["deferred_source_tests"].append(entry)
        problems = d3_sync_problems(manifest, broken, b2, ci)
        assert any("still deferred in B1" in p for p in problems), e
    for e in manifest["restored_b2_tests"]:
        broken_b2 = copy.deepcopy(b2)
        broken_b2["deferred_source_tests"].append(
            {
                "id": f"{e['file']}::{e['function']}",
                "cases": 1,
                "reason": "x",
                "owner_stage": "I-4-4D (x)",
                "rerun_condition": "x",
            }
        )
        problems = d3_sync_problems(manifest, b1, broken_b2, ci)
        assert any("still deferred in B2" in p for p in problems), e
    dropped = copy.deepcopy(b1)
    dropped["deferred_source_tests"] = []
    assert any("B1 deferred set differs" in p for p in d3_sync_problems(manifest, dropped, b2, ci))
    ownerless = copy.deepcopy(manifest)
    ownerless["remaining_b1_deferred"][0]["owner_stage"] = ""
    assert any("incomplete" in p for p in d3_sync_problems(ownerless, b1, b2, ci))


def test_ac4_rewritten_history_or_counts_are_detected(
    manifest: dict[str, Any], b1: dict[str, Any], b2: dict[str, Any]
) -> None:
    ci = CI.read_text(encoding="utf-8")
    assert d3_sync_problems(manifest, b1, b2, ci) == []
    rewritten_b1 = copy.deepcopy(b1)
    rewritten_b1["historical_pre_d3"]["ran"] = 122
    assert any("historical B1" in p for p in d3_sync_problems(manifest, rewritten_b1, b2, ci))
    erased_b1 = copy.deepcopy(b1)
    del erased_b1["historical_pre_d3"]
    assert any("historical B1" in p for p in d3_sync_problems(manifest, erased_b1, b2, ci))
    rewritten_d3 = copy.deepcopy(manifest)
    rewritten_d3["b2_counts"]["historical_pre_d3"]["ran"] = 368
    assert any("historical B2" in p for p in d3_sync_problems(rewritten_d3, b1, b2, ci))
    erased_b2 = copy.deepcopy(b2)
    del erased_b2["historical_pre_d3"]
    assert any("historical B2" in p for p in d3_sync_problems(manifest, b1, erased_b2, ci))
    rewritten_d2_history = copy.deepcopy(b2)
    rewritten_d2_history["historical_pre_d2"]["totals"]["ran"] = 366
    assert any(
        "368/298/70/22" in p for p in d3_sync_problems(manifest, b1, rewritten_d2_history, ci)
    )
    wrong_b1 = copy.deepcopy(b1)
    wrong_b1["totals"]["ran"] = 118
    assert any("B1 totals differ" in p for p in d3_sync_problems(manifest, wrong_b1, b2, ci))
    wrong_b2 = copy.deepcopy(b2)
    wrong_b2["totals"]["ran"] = 366
    assert any("B2 totals differ" in p for p in d3_sync_problems(manifest, b1, wrong_b2, ci))
    wrong_expected = copy.deepcopy(b2)
    first = next(iter(wrong_expected["expected_ran"]))
    wrong_expected["expected_ran"][first] -= 1
    assert any(
        "expected_ran does not sum" in p for p in d3_sync_problems(manifest, b1, wrong_expected, ci)
    )


def test_ac4_ci_count_tamper_and_lane_removal_are_detected(
    manifest: dict[str, Any], b1: dict[str, Any], b2: dict[str, Any]
) -> None:
    text = CI.read_text(encoding="utf-8")
    assert d3_sync_problems(manifest, b1, b2, text) == []
    for job, ran in (
        ("i4-4b1-vc-adjudication", b1["totals"]["ran"]),
        ("i4-4b2-vc-step4-tests", b2["totals"]["ran"]),
        (D3_JOB, manifest["totals"]["ran"]),
    ):
        pin = f"0 deferred with reasons, {ran} ran, 0 skipped"
        assert pin in (job_runs(text, job) or ""), job
        tampered = text.replace(pin, pin.replace(f"{ran} ran", f"{ran - 1} ran"))
        assert tampered != text
        assert any(
            f"does not pin the manifest ran count: {job}" in p
            for p in d3_sync_problems(manifest, b1, b2, tampered)
        )
    removed_lane = text.replace(f"  {D3_JOB}:", "  i4-4d3-renamed-away:")
    assert any(
        f"CI lane missing: {D3_JOB}" in p for p in d3_sync_problems(manifest, b1, b2, removed_lane)
    )
    other_manifest = text.replace(
        f"run_stage1b1_tests.py {LANES[D3_JOB]}", "run_stage1b1_tests.py docs/dev/x.json"
    )
    assert any(
        "does not run its manifest" in p for p in d3_sync_problems(manifest, b1, b2, other_manifest)
    )


def test_ac4_stale_absence_assertions_in_d1_d2_are_gone_and_detected(tmp_path: Path) -> None:
    assert stale_absence_problems(ROOT) == []
    for rel in (
        "tests/stage_i4/test_i4_4d1_preparation_step1.py",
        "tests/stage_i4/test_i4_4d2_step2_verification.py",
    ):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        stale = '    assert not (irl / "steps" / "step-4-pr-review.md").exists()\n'
        dest.write_text((ROOT / rel).read_text(encoding="utf-8") + stale, encoding="utf-8")
    assert len(stale_absence_problems(tmp_path)) == 2
    # the SKILL.md / Agent absence checks D1/D2 keep are not flagged
    keep = tmp_path / "tests/stage_i4/test_i4_4d1_preparation_step1.py"
    keep.write_text('    assert not (irl / "SKILL.md").exists()\n', encoding="utf-8")
    assert not any("d1_preparation" in p for p in stale_absence_problems(tmp_path))


def test_ac4_historical_documents_and_prior_manifests_are_not_rewritten() -> None:
    d1 = _json(D1_MANIFEST)
    d2m = _json(D2_MANIFEST)
    assert d2m["b2_counts"]["current"]["ran"] == 366  # D2-time record, kept as history
    assert d2m["b2_counts"]["historical_pre_d2"]["ran"] == 298
    assert d1["c2_counts"]
    for rel in (
        f"{IRL}/steps/step-2-verification.md",
        f"{IRL}/steps/preparation.md",
        f"{IRL}/steps/step-1-implementation.md",
    ):
        assert (ROOT / rel).is_file(), rel
