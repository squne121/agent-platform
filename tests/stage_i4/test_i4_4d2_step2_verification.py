"""I-4-4D2: Step 2 verification document + restored B2 deferred tests (#91).

Document wording tests, the real conversion code block of the migrated document, and tmp_path
only. Nothing here starts a SubAgent, calls live GitHub, publishes the Skill entrypoint or wires
Final Gate / `LOOP_HANDOFF_RESULT_V1`.

Test groups are selected by the Issue's Verification Commands with `-k test_ac<N>_`.
"""

from __future__ import annotations

import ast
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
MANIFEST = ROOT / "docs" / "dev" / "i4-4d2-step2-verification-manifest.json"
B2_MANIFEST = ROOT / "docs" / "dev" / "i4-4b2-vc-step4-tests-manifest.json"
B1_MANIFEST = ROOT / "docs" / "dev" / "i4-4b1-vc-adjudication-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
CI_JOB = "i4-4d2-step2-verification"
B2_CI_JOB = "i4-4b2-vc-step4-tests"
RUNNER_FRAGMENT = "run_stage1b1_tests.py docs/dev/i4-4d2-step2-verification-manifest.json"
ISSUE_SOURCE_COMMIT = "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
CP_FILE = ".claude/skills/impl-review-loop/tests/test_step4_pr_review_only_canonical_path.py"
CONVERSION_BLOCK = "step2-handoff-conversion-example"
# Historical I-4-4B2 (#79) measurement. It is never rewritten, only preserved.
B2_HISTORY = {
    "source_collected_before_removal": 368,
    "ran": 298,
    "removed_to_deferred_source_tests_cases": 70,
    "removed_functions": 22,
}

_SPEC = importlib.util.spec_from_file_location(
    "b2_closure_for_i4_4d2", ROOT / "tests" / "stage_i4" / "test_i4_4b2_closure.py"
)
assert _SPEC is not None and _SPEC.loader is not None
b2_closure = importlib.util.module_from_spec(_SPEC)
sys.modules["b2_closure_for_i4_4d2"] = b2_closure
_SPEC.loader.exec_module(b2_closure)


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_manifest())


@pytest.fixture
def b2() -> dict[str, Any]:
    return copy.deepcopy(json.loads(B2_MANIFEST.read_text(encoding="utf-8")))


@pytest.fixture
def b1() -> dict[str, Any]:
    return copy.deepcopy(json.loads(B1_MANIFEST.read_text(encoding="utf-8")))


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


def function_source(path: Path, name: str) -> str | None:
    """Exact source lines of a top-level test function, decorators included."""
    text = path.read_text(encoding="utf-8")
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            first = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            assert node.end_lineno is not None
            return "\n".join(text.splitlines()[first - 1 : node.end_lineno])
    return None


def restored_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Every restored function exists with its recorded body hash (no weakened assertion)."""
    problems: list[str] = []
    restored = m["restored_b2_tests"]
    if len(restored) != m["totals"]["restored_functions"]:
        problems.append("restored function count != totals")
    if sum(e["cases"] for e in restored) != m["totals"]["restored_cases"]:
        problems.append("restored case count != totals")
    for e in restored:
        src = function_source(root / CP_FILE, e["function"])
        if src is None:
            problems.append(f"restored function missing: {e['function']}")
        elif hashlib.sha256(src.encode("utf-8")).hexdigest() != e["body_sha256"]:
            problems.append(f"restored function body differs from the source: {e['function']}")
    return problems


def b2_sync_problems(
    m: dict[str, Any], b2_manifest: dict[str, Any], b1_manifest: dict[str, Any], root: Path, ci: str
) -> list[str]:
    """B2 must no longer defer the restored tests, keep the two remaining and its history."""
    problems: list[str] = []
    deferred = {e["id"]: e for e in b2_manifest["deferred_source_tests"]}
    for e in m["restored_b2_tests"]:
        if f"test_step4_pr_review_only_canonical_path.py::{e['function']}" in deferred:
            problems.append(f"restored test is still deferred in B2: {e['function']}")
    remaining = {e["id"]: e for e in m["remaining_b2_deferred"]}
    if set(remaining) != set(deferred):
        problems.append("remaining deferred set differs from the B2 deferred_source_tests")
    for e in deferred.values():
        for key in ("reason", "owner_stage", "rerun_condition"):
            if not e.get(key):
                problems.append(f"remaining deferred entry lost {key}: {e['id']}")
    cur = m["b2_counts"]["current"]
    t = b2_manifest["totals"]
    if (
        t["ran"],
        t["removed_functions"],
        t["removed_to_deferred_source_tests_cases"],
        t["source_collected_before_removal"],
        t["skipped"],
    ) != (
        cur["ran"],
        cur["deferred_functions"],
        cur["deferred_cases"],
        cur["source_collected"],
        cur["skipped"],
    ):
        problems.append("B2 totals differ from the current counts recorded by D2")
    if sum(b2_manifest["expected_ran"].values()) != t["ran"]:
        problems.append("B2 expected_ran does not sum to totals.ran")
    if b2_manifest.get("historical_pre_d2", {}).get("totals", {}).get(
        "source_collected_before_removal"
    ) != B2_HISTORY["source_collected_before_removal"] or any(
        b2_manifest.get("historical_pre_d2", {}).get("totals", {}).get(k) != v
        for k, v in B2_HISTORY.items()
    ):
        problems.append("the historical B2 measurement (368/298/70/22) was lost or rewritten")
    hist = m["b2_counts"]["historical_pre_d2"]
    if any(hist.get(k) != v for k, v in B2_HISTORY.items()):
        problems.append("D2 historical record differs from the B2 measurement")
    if f"0 deferred with reasons, {t['ran']} ran, 0 skipped" not in ci:
        problems.append("the B2 CI lane does not pin the current ran count")
    problems.extend(b2_closure.deferred_problems(b2_manifest, b1_manifest, root / b2_closure.TESTS))
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


def test_ac1_document_reproduces_the_pinned_source(manifest: dict[str, Any]) -> None:
    assert document_problems(manifest, ROOT) == []
    assert list(manifest["documents"]) == [
        ".claude/skills/impl-review-loop/steps/step-2-verification.md"
    ]


def test_ac1_references_are_classified_with_an_owner_and_really_absent(
    manifest: dict[str, Any],
) -> None:
    refs = manifest["unmigrated_references"]
    assert refs["later_i_4_4d_slices"] and refs["legacy_repository_specific"]
    text = "\n".join(refs["later_i_4_4d_slices"])
    for needed in (
        "step-4-pr-review.md",
        "step-5-feedback-and-termination.md",
        "test-runner",
        "body_only_repair_plan.py",
        "SKILL.md",
    ):
        assert needed in text, needed
    # nothing the document links to is faked: the unmigrated targets genuinely do not exist
    irl = ROOT / ".claude" / "skills" / "impl-review-loop"
    assert not (irl / "SKILL.md").exists()
    assert not (irl / "steps" / "step-4-pr-review.md").exists()
    assert not (irl / "steps" / "step-5-feedback-and-termination.md").exists()
    assert not (ROOT / ".claude" / "agents" / "test-runner.md").exists()
    assert not list((irl / "scripts").glob("body_only_repair_plan.py"))
    # the migrated dependencies are real
    for rel in manifest["migrated_dependencies"]:
        assert (ROOT / rel).is_file(), rel


def test_ac1_boundaries_are_not_overclaimed(manifest: dict[str, Any]) -> None:
    for key, value in manifest["evidence_boundary"].items():
        if key != "kind":
            assert value is False, key


# --- AC2: the restored tests run for real ------------------------------------------------


def test_ac2_restored_functions_keep_their_source_bodies(manifest: dict[str, Any]) -> None:
    assert restored_problems(manifest, ROOT) == []
    names = [e["function"] for e in manifest["restored_b2_tests"]]
    assert len(names) == 20 and len(set(names)) == 20
    assert sum(e["cases"] for e in manifest["restored_b2_tests"]) == 68


def test_ac2_document_has_the_conversion_block_the_restored_tests_execute() -> None:
    doc = (ROOT / ".claude/skills/impl-review-loop/steps/step-2-verification.md").read_text(
        encoding="utf-8"
    )
    assert doc.count(CONVERSION_BLOCK) >= 1
    assert "TEST_VERDICT_MACHINE/v2" in doc


def test_ac2_restored_functions_collect_and_pass_on_the_real_document(
    manifest: dict[str, Any],
) -> None:
    ids = [f"{CP_FILE}::{e['function']}" for e in manifest["restored_b2_tests"]]
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rsxX"]
    collected = subprocess.run(
        [*cmd, "--collect-only", "-q", *ids],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    per_function: dict[str, int] = {}
    for line in collected.stdout.splitlines():
        if "::" in line:
            name = line.split("::")[1].split("[")[0]
            per_function[name] = per_function.get(name, 0) + 1
    assert per_function == {e["function"]: e["cases"] for e in manifest["restored_b2_tests"]}
    r = subprocess.run([*cmd, "-q", *ids], cwd=ROOT, capture_output=True, text=True, check=False)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "68 passed" in out, out
    assert not any(w in out for w in ("skipped", "xfailed", "xpassed", "deselected")), out


# --- AC3: B2 deferred inventory and history ----------------------------------------------


def test_ac3_b2_sync_keeps_remaining_deferred_and_history(
    manifest: dict[str, Any], b2: dict[str, Any], b1: dict[str, Any]
) -> None:
    ci = CI.read_text(encoding="utf-8")
    assert b2_sync_problems(manifest, b2, b1, ROOT, ci) == []
    assert sorted(e["id"] for e in b2["deferred_source_tests"]) == [
        "test_step4_contract_snapshot_handoff.py::test_step4_doc_names_the_new_handoff_arguments",
        "test_step4_pr_review_only_canonical_path.py::test_step_docs_state_canonical_entrance",
    ]
    cur = manifest["b2_counts"]["current"]
    assert (cur["ran"], cur["deferred_functions"], cur["deferred_cases"]) == (366, 2, 2)


def test_ac3_current_counts_are_measured_not_assumed(manifest: dict[str, Any]) -> None:
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            *json.loads(B2_MANIFEST.read_text(encoding="utf-8"))["test_files"],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{manifest['b2_counts']['current']['ran']} tests collected" in r.stdout


def test_ac3_prior_slices_still_hold_after_the_step2_document_exists() -> None:
    """D1 asserts the absence of other documents only; the Step 2 document must not collide."""
    d1 = ROOT / "docs" / "dev" / "i4-4d1-preparation-step1-manifest.json"
    d1_manifest = json.loads(d1.read_text(encoding="utf-8"))
    assert (
        ".claude/skills/impl-review-loop/steps/step-2-verification.md"
        not in (d1_manifest["documents"])
    )
    text = "\n".join(d1_manifest["unmigrated_references"]["later_i_4_4d_slices"])
    assert "step-4-pr-review.md" in text


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


def test_ac4_missing_or_weakened_restored_function_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    dest = tmp_path / CP_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    original = (ROOT / CP_FILE).read_text(encoding="utf-8")
    dest.write_text(original, encoding="utf-8")
    assert restored_problems(manifest, tmp_path) == []
    name = manifest["restored_b2_tests"][0]["function"]
    # an assertion silently weakened inside a restored function
    src = function_source(dest, name)
    assert src is not None
    dest.write_text(original.replace(src, src.replace("assert ", "assert True or ", 1)), "utf-8")
    assert any("body differs" in p for p in restored_problems(manifest, tmp_path))
    # a restored function dropped altogether
    dest.write_text(original.replace(f"def {name}(", f"def renamed_{name}(", 1), "utf-8")
    assert any("restored function missing" in p for p in restored_problems(manifest, tmp_path))


def test_ac4_unrestored_test_is_detected(
    manifest: dict[str, Any], b2: dict[str, Any], b1: dict[str, Any]
) -> None:
    ci = CI.read_text(encoding="utf-8")
    template = next(e for e in b2["deferred_source_tests"] if "canonical_entrance" in e["id"])
    for restored in manifest["restored_b2_tests"]:
        broken = copy.deepcopy(b2)
        entry = copy.deepcopy(template)
        entry["id"] = f"test_step4_pr_review_only_canonical_path.py::{restored['function']}"
        broken["deferred_source_tests"].append(entry)
        assert any(
            "still deferred in B2" in p for p in b2_sync_problems(manifest, broken, b1, ROOT, ci)
        )


def test_ac4_stale_deferred_entry_is_detected(
    manifest: dict[str, Any], b2: dict[str, Any], b1: dict[str, Any]
) -> None:
    ci = CI.read_text(encoding="utf-8")
    stale = copy.deepcopy(b2)
    stale["deferred_source_tests"][0]["id"] = "test_step4_contract_snapshot_handoff.py::gone"
    problems = b2_sync_problems(manifest, stale, b1, ROOT, ci)
    assert any("remaining deferred set differs" in p for p in problems)
    reasonless = copy.deepcopy(b2)
    reasonless["deferred_source_tests"][0]["reason"] = ""
    assert any("lost reason" in p for p in b2_sync_problems(manifest, reasonless, b1, ROOT, ci))


def test_ac4_dropped_remaining_deferred_is_detected(
    manifest: dict[str, Any], b2: dict[str, Any], b1: dict[str, Any]
) -> None:
    ci = CI.read_text(encoding="utf-8")
    for index in (0, 1):
        broken = copy.deepcopy(b2)
        del broken["deferred_source_tests"][index]
        problems = b2_sync_problems(manifest, broken, b1, ROOT, ci)
        assert any("remaining deferred set differs" in p for p in problems)


def test_ac4_rewritten_history_or_counts_are_detected(
    manifest: dict[str, Any], b2: dict[str, Any], b1: dict[str, Any]
) -> None:
    ci = CI.read_text(encoding="utf-8")
    rewritten = copy.deepcopy(b2)
    rewritten["historical_pre_d2"]["totals"]["ran"] = 366
    assert any(
        "historical B2 measurement" in p
        for p in b2_sync_problems(manifest, rewritten, b1, ROOT, ci)
    )
    erased = copy.deepcopy(b2)
    del erased["historical_pre_d2"]
    assert any(
        "historical B2 measurement" in p for p in b2_sync_problems(manifest, erased, b1, ROOT, ci)
    )
    wrong_totals = copy.deepcopy(b2)
    wrong_totals["totals"]["ran"] = 298
    assert any(
        "B2 totals differ" in p for p in b2_sync_problems(manifest, wrong_totals, b1, ROOT, ci)
    )
    wrong_expected = copy.deepcopy(b2)
    wrong_expected["expected_ran"][CP_FILE] -= 1
    assert any(
        "expected_ran does not sum" in p
        for p in b2_sync_problems(manifest, wrong_expected, b1, ROOT, ci)
    )


def test_ac4_ci_count_tamper_and_lane_removal_are_detected(
    manifest: dict[str, Any], b2: dict[str, Any], b1: dict[str, Any]
) -> None:
    text = CI.read_text(encoding="utf-8")
    assert ci_lane_problems(text) == []
    assert ci_lane_problems(text.replace(RUNNER_FRAGMENT, "x")) != []
    assert ci_lane_problems("jobs: {}") != []
    stale_ci = text.replace("0 deferred with reasons, 366 ran", "0 deferred with reasons, 298 ran")
    assert any(
        "does not pin the current ran count" in p
        for p in b2_sync_problems(manifest, b2, b1, ROOT, stale_ci)
    )
    assert B2_CI_JOB in (yaml.safe_load(text) or {}).get("jobs", {})
