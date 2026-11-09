"""I-4-4D1: Preparation / Step 1 steps documents + restored C2 deferred tests (#89).

Document wording tests and tmp_path only. Nothing here starts a SubAgent, calls live GitHub,
publishes the Skill entrypoint or wires Final Gate / `LOOP_HANDOFF_RESULT_V1`.

Test groups are selected by the Issue's Verification Commands with `-k test_ac<N>_`.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-4d1-preparation-step1-manifest.json"
C2_MANIFEST = ROOT / "docs" / "dev" / "i4-4c2-intake-capsule-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
CI_JOB = "i4-4d1-preparation-step1"
RUNNER_FRAGMENT = "run_stage1b1_tests.py docs/dev/i4-4d1-preparation-step1-manifest.json"
ISSUE_SOURCE_COMMIT = "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"

_SPEC = importlib.util.spec_from_file_location(
    "c2_closure_for_i4_4d1", ROOT / "tests" / "stage_i4" / "test_i4_4c2_closure.py"
)
assert _SPEC is not None and _SPEC.loader is not None
c2_closure = importlib.util.module_from_spec(_SPEC)
sys.modules["c2_closure_for_i4_4d1"] = c2_closure
_SPEC.loader.exec_module(c2_closure)


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_manifest())


@pytest.fixture
def c2() -> dict[str, Any]:
    return copy.deepcopy(json.loads(C2_MANIFEST.read_text(encoding="utf-8")))


def _git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def document_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Parity of every migrated document against its recorded source and intentional diff."""
    problems: list[str] = []
    if m["source"]["commit"] != ISSUE_SOURCE_COMMIT:
        problems.append("source commit pin is not the peeled commit of the source tag")
    if not m["documents"]:
        problems.append("no documents recorded")
    for rel, rec in m["documents"].items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing document: {rel}")
            continue
        moved = path.read_bytes()
        if hashlib.sha256(moved).hexdigest() != rec["moved_sha256"]:
            problems.append(f"moved_sha256 mismatch: {rel}")
        if moved.count(b"\n") != rec["moved_lines"]:
            problems.append(f"moved_lines mismatch: {rel}")
        if rec["verbatim"] or not rec["intentional_diff"]:
            problems.append(f"intentional_diff not recorded: {rel}")
        text = moved.decode("utf-8")
        for edit in rec["intentional_diff"]:
            if text.count(edit["moved"]) != 1:
                problems.append(f"intentional edit not found exactly once: {rel}")
                break
            text = text.replace(edit["moved"], edit["source"], 1)
        source = text.encode("utf-8")
        if hashlib.sha256(source).hexdigest() != rec["source_sha256"]:
            problems.append(f"source_sha256 not reproducible from recorded edits: {rel}")
        if _git_blob_sha1(source) != rec["source_blob_sha1"]:
            problems.append(f"source blob SHA not reproducible from recorded edits: {rel}")
    return problems


def c2_sync_problems(m: dict[str, Any], c2_manifest: dict[str, Any], root: Path) -> list[str]:
    """C2 must no longer defer the five restored tests and must keep the two remaining ones."""
    problems: list[str] = []
    deferred_ids = {e["id"] for e in c2_manifest["deferred_tests"]}
    source_ids = {e["id"] for e in c2_manifest["deferred_source_tests"]}
    for tid in m["restored_c2_tests"]:
        if tid in deferred_ids or tid in source_ids:
            problems.append(f"restored test is still deferred in C2: {tid}")
    remaining = {e["id"]: e for e in m["remaining_c2_deferred"]}
    if set(remaining) != deferred_ids:
        problems.append("remaining deferred set differs from the C2 deferred_tests")
    for e in c2_manifest["deferred_tests"]:
        for key in ("reason", "owner_stage", "rerun_condition"):
            if not e.get(key):
                problems.append(f"remaining deferred entry lost {key}: {e['id']}")
    kinds = {e["kind"] for e in c2_manifest["deferred_tests"]}
    if kinds != {"unmigrated_document", "live_github_canary"}:
        problems.append("the SKILL.md entry and the live canary must both remain deferred")
    cur = m["c2_counts"]["current"]
    t = c2_manifest["totals"]
    if (t["source_collected"], t["ran"], t["deferred"], t["skipped"]) != (
        cur["source_collected"],
        cur["ran"],
        cur["deferred"],
        cur["skipped"],
    ):
        problems.append("C2 totals differ from the current counts recorded by D1")
    if c2_manifest.get("historical_pre_d1", {}).get("totals") != {
        "source_test_functions": 119,
        "source_collected": 119,
        "deferred": 7,
        "ran": 112,
        "skipped": 0,
        "xfailed": 0,
    }:
        problems.append("the historical C2 measurement (119/112/7) was lost or rewritten")
    hist = m["c2_counts"]["historical_pre_d1"]
    if (hist["source_collected"], hist["ran"], hist["deferred"]) != (119, 112, 7):
        problems.append("D1 historical record differs from the C2 measurement")
    problems.extend(c2_closure.deferred_problems(c2_manifest, root))
    return problems


def ci_lane_problems(ci_text: str) -> list[str]:
    jobs = (yaml.safe_load(ci_text) or {}).get("jobs", {})
    job = jobs.get(CI_JOB)
    if not isinstance(job, dict):
        return [f"job {CI_JOB} missing"]
    runs = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    required = (
        RUNNER_FRAGMENT,
        "(skipped|xfailed|xpassed|deselected)",
        "0 deferred with reasons",
        "tests/stage_i4",
    )
    return [f"CI step missing: {frag}" for frag in required if frag not in runs]


# --- AC1: source parity and intentional diff ---------------------------------------------


def test_ac1_documents_reproduce_the_pinned_source(manifest: dict[str, Any]) -> None:
    assert document_problems(manifest, ROOT) == []


def test_ac1_references_are_classified_with_an_owner(manifest: dict[str, Any]) -> None:
    refs = manifest["unmigrated_references"]
    assert refs["later_i_4_4d_slices"] and refs["legacy_repository_specific"]
    text = "\n".join(refs["later_i_4_4d_slices"])
    for needed in ("SKILL.md", "step-4-pr-review.md", "implement-issue", "scope-rollup-runner"):
        assert needed in text, needed
    # every unmigrated Skill the documents link to is genuinely absent, never faked
    irl = ROOT / ".claude" / "skills" / "impl-review-loop"
    assert not (irl / "SKILL.md").exists()
    # step-4-pr-review.md was migrated later by I-4-4D3 (#93); the classification above is the
    # D1-time record and stays as history. SKILL.md and implement-issue are still genuinely absent.
    assert (irl / "steps" / "step-4-pr-review.md").is_file()
    assert not (ROOT / ".claude" / "skills" / "implement-issue").exists()


def test_ac1_boundaries_are_not_overclaimed(manifest: dict[str, Any]) -> None:
    for key, value in manifest["evidence_boundary"].items():
        if key != "kind":
            assert value is False, key


# --- AC2: the five restored tests run for real -------------------------------------------


def test_ac2_five_restored_c2_tests_pass_on_the_real_documents(manifest: dict[str, Any]) -> None:
    ids = manifest["restored_c2_tests"]
    assert len(ids) == 5 and len(set(ids)) == 5
    for tid in ids:
        assert (ROOT / tid.split("::")[0]).is_file(), tid
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rsxX", *ids],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "5 passed" in out, out
    assert not any(w in out for w in ("skipped", "xfailed", "xpassed", "deselected")), out


# --- AC3: remaining deferred and historical numbers --------------------------------------


def test_ac3_remaining_deferred_keep_reason_owner_and_history(
    manifest: dict[str, Any], c2: dict[str, Any]
) -> None:
    assert c2_sync_problems(manifest, c2, ROOT) == []
    assert [e["kind"] for e in c2["deferred_tests"]] == [
        "unmigrated_document",
        "live_github_canary",
    ]
    assert c2["deferred_tests"][0]["requires_missing_files"] == [
        ".claude/skills/impl-review-loop/SKILL.md"
    ]


# --- AC4: negative controls --------------------------------------------------------------


def test_ac4_missing_document_is_detected(manifest: dict[str, Any], tmp_path: Path) -> None:
    assert any("missing document" in p for p in document_problems(manifest, tmp_path))


def test_ac4_tampered_document_is_detected(manifest: dict[str, Any], tmp_path: Path) -> None:
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


def test_ac4_wrong_source_pin_is_detected(manifest: dict[str, Any]) -> None:
    wrong_sha = copy.deepcopy(manifest)
    first = next(iter(wrong_sha["documents"]))
    wrong_sha["documents"][first]["source_blob_sha1"] = "0" * 40
    assert any("source blob SHA" in p for p in document_problems(wrong_sha, ROOT))
    wrong_commit = copy.deepcopy(manifest)
    wrong_commit["source"]["commit"] = "0" * 40
    assert any("source commit pin" in p for p in document_problems(wrong_commit, ROOT))
    hollow = copy.deepcopy(manifest)
    hollow["documents"][first]["intentional_diff"] = []
    assert any("intentional_diff not recorded" in p for p in document_problems(hollow, ROOT))


def test_ac4_unrestored_test_is_detected(manifest: dict[str, Any], c2: dict[str, Any]) -> None:
    template = next(e for e in c2["deferred_tests"] if e["kind"] == "unmigrated_document")
    for tid in manifest["restored_c2_tests"]:
        broken = copy.deepcopy(c2)
        entry = copy.deepcopy(template)
        entry["id"] = tid
        entry["file"], entry["function"] = tid.split("::")
        broken["deferred_tests"].append(entry)
        broken["deferred_source_tests"].append(copy.deepcopy(entry))
        assert any("still deferred in C2" in p for p in c2_sync_problems(manifest, broken, ROOT))


def test_ac4_stale_deferred_document_is_detected(
    manifest: dict[str, Any], c2: dict[str, Any], tmp_path: Path
) -> None:
    template = next(e for e in c2["deferred_tests"] if e["kind"] == "unmigrated_document")
    stale = copy.deepcopy(c2)
    entry = copy.deepcopy(template)
    entry["requires_missing_files"] = [next(iter(manifest["documents"]))]
    stale["deferred_tests"][0] = entry
    assert any("stale deferred entry" in p for p in c2_sync_problems(manifest, stale, ROOT))


def test_ac4_dropped_remaining_deferred_is_detected(
    manifest: dict[str, Any], c2: dict[str, Any]
) -> None:
    for index in (0, 1):
        broken = copy.deepcopy(c2)
        del broken["deferred_tests"][index]
        del broken["deferred_source_tests"][index]
        assert any("remaining deferred" in p for p in c2_sync_problems(manifest, broken, ROOT))
    reasonless = copy.deepcopy(c2)
    reasonless["deferred_tests"][0]["reason"] = ""
    assert any("lost reason" in p for p in c2_sync_problems(manifest, reasonless, ROOT))


def test_ac4_rewritten_history_is_detected(manifest: dict[str, Any], c2: dict[str, Any]) -> None:
    rewritten = copy.deepcopy(c2)
    rewritten["historical_pre_d1"]["totals"]["deferred"] = 2
    assert any(
        "historical C2 measurement" in p for p in c2_sync_problems(manifest, rewritten, ROOT)
    )
    wrong_totals = copy.deepcopy(c2)
    wrong_totals["totals"]["ran"] = 112
    assert any("C2 totals differ" in p for p in c2_sync_problems(manifest, wrong_totals, ROOT))


def test_ac4_ci_lane_is_wired_and_guard_removal_is_detected() -> None:
    text = CI.read_text(encoding="utf-8")
    assert ci_lane_problems(text) == []
    assert ci_lane_problems(text.replace(RUNNER_FRAGMENT, "x")) != []
    assert ci_lane_problems("jobs: {}") != []
