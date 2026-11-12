"""I-4-4D5: pr-review-judge Skill, pr-reviewer-lite Agent and the restored 3-consumer test (#97).

Static parity against the pinned source, plus the real, unchanged source test under pytest.
Nothing here starts an Agent or SubAgent, runs a review, publishes a verdict, calls live GitHub,
mutates a Git ref, releases the temporary ``disable-model-invocation`` or wires Final Gate /
``LOOP_HANDOFF_RESULT_V1``. A document-parity or reference-string PASS is not evidence that the
Skill can be preloaded or that the Agent works.

Test groups are selected by the Issue's Verification Commands with ``-k test_ac<N>_``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d5-pr-review-consumers-manifest.json"
D4_MANIFEST = DEV / "i4-4d4-skill-agents-readonly-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = "tests/stage_i4/test_i4_4d5_pr_review_consumers.py"
D5_JOB = "i4-4d5-pr-review-consumers"
D5_MANIFEST_REL = "docs/dev/i4-4d5-pr-review-consumers-manifest.json"
SK = ".claude/skills/pr-review-judge"
SKILL = f"{SK}/SKILL.md"
LITE = ".claude/agents/pr-reviewer-lite.md"
STEP4 = ".claude/skills/impl-review-loop/steps/step-4-pr-review.md"
SOURCE_TEST = ".claude/skills/impl-review-loop/tests/test_wait_ci_checks.py"
RESTORED_FN = "test_canonical_evaluator_consumers_use_same_function"
RESTORED_ID = f"{SOURCE_TEST}::{RESTORED_FN}"
SOURCE_COMMIT = "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
# Pins fixed by the Issue contract (#97): the source blobs of the Skill and the lite Agent.
ISSUE_SOURCE_BLOBS = {
    SKILL: "db2ae9d1f17498fa358528f552974f7e2666c8fa",
    LITE: "7216a760618bc33e90bbcea3ea59dac962e467c5",
}
EDITING_TOOLS = {"Edit", "Write", "MultiEdit"}
# Not migrated in D5: they stay absent (no empty stub) and keep an owner in the manifest.
# ci_verdict_summary.py and check_pr_review_gates.py left this list when I-4-4D6 (#99) migrated
# them (their pre-D6 record is kept in the manifest as historical_pre_d6).
STILL_ABSENT = (
    f"{SK}/scripts/ci_verdict_summary_v2.py",
    # I-4-4D7 (#101) migrated validate_pr_body.py; I-4-4D11 (#108) migrated open-pr/SKILL.md and
    # scripts/open_pr.py. Only the updater stays absent.
    ".claude/skills/open-pr/scripts/update_pr.py",
    ".claude/skills/implement-issue",
    ".claude/hooks/pr_reviewer_guard.py",
)


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


d4 = _load("tests/stage_i4/test_i4_4d4_skill_agents_readonly.py", "d4_helpers_for_i4_4d5")
parity_problems = d4.parity_problems
frontmatter = d4.frontmatter
job_runs = d4.job_runs
function_source = d4.function_source


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


@pytest.fixture
def d4_manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(D4_MANIFEST))


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def _copy_tree(manifest: dict[str, Any], tmp_path: Path) -> None:
    rels = [
        *manifest["documents"],
        *manifest["verbatim_files"],
        SOURCE_TEST,
        STEP4,
        ".claude/agents/pr-reviewer.md",
    ]
    for rel in rels:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)


# --- problem functions (pure: they return a list of messages, [] means OK) -----------------


def issue_pin_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if m["source"]["commit"] != SOURCE_COMMIT:
        problems.append("source commit pin is not the peeled commit of the source tag")
    for rel, blob in ISSUE_SOURCE_BLOBS.items():
        if m["documents"].get(rel, {}).get("source_blob_sha1") != blob:
            problems.append(f"source blob is not the Issue-fixed pin: {rel}")
    return problems


def reference_map_problems(m: dict[str, Any], root: Path) -> list[str]:
    """The 10 references the Skill's Reference Loading Map points to are exactly the migrated set."""
    skill = (root / SKILL).read_text(encoding="utf-8") if (root / SKILL).is_file() else ""
    section = skill.split("## Reference Loading Map", 1)[-1].split("\n## ", 1)[0]
    named = set(re.findall(r"([a-z0-9-]+\.md)", section))
    migrated = {Path(p).name for p in m["verbatim_files"]}
    problems: list[str] = []
    if len(migrated) != 10:
        problems.append("expected exactly 10 migrated references")
    if not migrated <= named:
        problems.append(f"Reference Loading Map no longer names: {sorted(migrated - named)}")
    refs_dir = root / SK / "references"
    present = {f.name for f in refs_dir.iterdir()} if refs_dir.is_dir() else set()
    # I-4-4D6 migrated the result schema (it is not a Reference Loading Map document).
    for name in sorted(present - migrated - {"pr-review-gate-result-schema.yml"}):
        problems.append(f"unrecorded reference present: {name}")
    return problems


def frontmatter_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Skill is temporarily disabled and says so; the lite Agent stays read-only."""
    problems: list[str] = []
    exp = m["frontmatter"]
    if not (root / SKILL).is_file() or not (root / LITE).is_file():
        return ["missing Skill or lite Agent"]
    fm = frontmatter(root / SKILL)
    if fm.get("name") != "pr-review-judge":
        problems.append("Skill name changed")
    if fm.get("disable-model-invocation") is not True:
        problems.append("Skill must carry disable-model-invocation: true (temporary)")
    body = (root / SKILL).read_text(encoding="utf-8").split("---", 2)[2]
    for needle in ("disable-model-invocation", "preload", "手動", "dispatch", "未移管"):
        if needle not in body:
            problems.append(f"Skill body lost the fail-closed explanation: {needle}")
    if "I-4-4D5" not in body:
        problems.append("Skill body lost its migration-status note")
    lite = frontmatter(root / LITE)
    want = exp[LITE]
    for key in ("name", "tools", "disallowedTools", "model", "effort", "permissionMode", "skills"):
        if lite.get(key) != want.get(key):
            problems.append(f"lite Agent frontmatter {key} differs from the pinned source")
    if set(lite.get("tools") or []) & EDITING_TOOLS:
        problems.append("lite Agent gained an editing tool")
    if not (EDITING_TOOLS | {"Agent", "Skill"}) <= set(lite.get("disallowedTools") or []):
        problems.append("lite Agent lost a disallowedTools entry")
    if lite.get("permissionMode") != "dontAsk":
        problems.append("lite Agent permissionMode is no longer dontAsk")
    if lite.get("hooks"):
        problems.append("lite Agent must not depend on an unmigrated hook")
    text = (root / LITE).read_text(encoding="utf-8")
    if "I-4-4D5" not in text or "dispatch しない" not in text:
        problems.append("lite Agent lost its no-dispatch note")
    reviewer = root / ".claude/agents/pr-reviewer.md"
    if reviewer.is_file() and frontmatter(reviewer).get("skills") != ["pr-review-judge"]:
        problems.append("pr-reviewer.md skills declaration was changed (D5 must not edit it)")
    return problems


def deny_list_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Python-repo code / dependency changes must not be treated as docs-only lite reviews."""
    text = (root / LITE).read_text(encoding="utf-8") if (root / LITE).is_file() else ""
    match = re.search(r"```yaml\ndeny_if_any:\n(.*?)```", text, re.DOTALL)
    if match is None:
        return ["deny_if_any block not found in the lite Agent"]
    parsed = yaml.safe_load("deny_if_any:\n" + match.group(1))["deny_if_any"]
    patterns: list[str] = []
    for item in parsed:
        patterns += list(
            (item or {}).get("changed_path_matches", []) if isinstance(item, dict) else []
        )
    problems = [
        f"deny-list does not cover {need}"
        for need in m["deny_list_must_include"]
        if need not in patterns
    ]
    for legacy in ("pnpm-lock.yaml", "package.json"):
        if legacy in patterns:
            problems.append(f"deny-list still carries the legacy {legacy}")
    return problems


def consumer_problems(root: Path) -> list[str]:
    """The three consumers exist and name the one canonical evaluator."""
    problems: list[str] = []
    for rel in (SKILL, LITE, STEP4):
        path = root / rel
        if not path.is_file():
            problems.append(f"consumer missing: {rel}")
        elif "wait_ci_checks" not in path.read_text(encoding="utf-8"):
            problems.append(f"consumer does not reference wait_ci_checks: {rel}")
    return problems


def restoration_problems(m: dict[str, Any], d4m: dict[str, Any], root: Path) -> list[str]:
    """The deferral is released and the source assertion is unchanged (byte parity via D4)."""
    problems: list[str] = []
    if d4m["deferred_tests"] or m["deferred_tests"]:
        problems.append("a deferral is still recorded although the 3 consumers exist")
    for label, man in (("D4", d4m), ("D5", m)):
        t = man["totals"]
        if (t["source_collected"], t["deferred"], t["ran"], t["skipped"], t["xfailed"]) != (
            108,
            0,
            108,
            0,
            0,
        ):
            problems.append(f"{label} totals are not 108 collected / 0 deferred / 108 ran")
        if man["expected_ran"] != man["expected_collect"]:
            problems.append(f"{label} expected_ran != expected_collect")
    hist = d4m.get("historical_pre_d5", {})
    hist_totals = {
        k: hist.get("totals", {}).get(k) for k in ("source_collected", "deferred", "ran", "skipped")
    }
    if hist_totals != {
        "source_collected": 108,
        "deferred": 1,
        "ran": 107,
        "skipped": 0,
    } or not hist.get("deferred_tests"):
        problems.append("D4 historical_pre_d5 (108 / 1 / 107) was lost or rewritten")
    if d4m.get("restored_by_d5", {}).get("id") != RESTORED_ID:
        problems.append("D4 restored_by_d5 does not track the original function id")
    if m["d4_restoration"]["historical_pre_d5"] != hist:
        problems.append("D5 manifest historical_pre_d5 differs from the D4 record")
    problems += [f"D4 source parity: {p}" for p in parity_problems(d4m, root)]
    body = (
        function_source(root / SOURCE_TEST, RESTORED_FN) if (root / SOURCE_TEST).is_file() else None
    )
    if body is None:
        problems.append("restored source function is missing")
    else:
        for needle in (
            "pr-review-judge",
            "pr-reviewer-lite.md",
            "step-4-pr-review.md",
            "is_file()",
        ):
            if needle not in body:
                problems.append(f"restored source test lost its assertion: {needle}")
        if '"wait_ci_checks" in text' not in body:
            problems.append("restored source test lost the wait_ci_checks assertion")
    for rel in d4m["test_files"]:
        text = (root / rel).read_text(encoding="utf-8") if (root / rel).is_file() else ""
        if d4.SKIP_RE.search(text):
            problems.append(f"skip / xfail marker in a source test file: {rel}")
    return problems


def stale_absence_problems(root: Path) -> list[str]:
    """No earlier slice test may still assert that the D5 files do not exist."""
    problems: list[str] = []
    for rel in sorted((root / "tests" / "stage_i4").glob("test_i4_4d[1-4]_*.py")):
        text = rel.read_text(encoding="utf-8")
        for m in re.finditer(r"for absent in \((.*?)\):", text, re.DOTALL):
            if "pr-review-judge" in m.group(1) or "pr-reviewer-lite" in m.group(1):
                problems.append(f"stale absence assertion in {rel.name}")
        if re.search(r"assert not [^\n]*(pr-review-judge|pr-reviewer-lite)", text):
            problems.append(f"stale absence assertion in {rel.name}")
    return problems


def dependency_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Unmigrated hard dependencies keep an owner and a rerun condition and stay really absent."""
    problems: list[str] = []
    deps = m["unmigrated_references"]["hard_dependencies"]
    if not deps:
        problems.append("no unmigrated hard dependency recorded")
    for e in deps:
        for key in ("reference", "owner", "reason", "rerun_condition"):
            if not e.get(key):
                problems.append(f"unmigrated dependency without {key}: {e.get('reference')}")
    text = "\n".join(e["reference"] for e in deps)
    for needed in ("ci_verdict_summary_v2.py", "open-pr", "pr_reviewer_guard.py", "contract_drift"):
        if needed not in text:
            problems.append(f"unmigrated dependency not recorded: {needed}")
    for absent in STILL_ABSENT:
        if (root / absent).exists():
            problems.append(f"recorded as unmigrated but present (update the record): {absent}")
    return problems


def boundary_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    b = m["evidence_boundary"]
    for key in (
        "live_e2e",
        "agent_dispatched",
        "skill_production_invocable",
        "skill_preload_effective",
        "verdict_published",
        "final_gate_wired",
        "loop_handoff_result_v1_wired",
        "github_live_operations",
        "git_ref_mutation",
        "hook_effective_protection",
    ):
        if b.get(key) is not False:
            problems.append(f"evidence boundary overclaims: {key}")
    decisions = json.dumps(m["decisions"])
    for needle in ("temporary", "does not preload", "not proof"):
        if needle not in decisions:
            problems.append(f"decisions lost: {needle}")
    return problems


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    runs = job_runs(ci_text, D5_JOB)
    if runs is None:
        return [f"CI lane missing: {D5_JOB}"]
    problems: list[str] = []
    if f"run_stage1b1_tests.py {D5_MANIFEST_REL}" not in runs:
        problems.append("CI lane does not run its manifest")
    t = m["totals"]
    if f"{t['deferred']} deferred with reasons, {t['ran']} ran, 0 skipped" not in runs:
        problems.append("CI lane does not pin the manifest ran count")
    if "tests/stage_i4" not in runs:
        problems.append("CI lane does not run the closure tests")
    if "(skipped|xfailed|xpassed|deselected)" not in runs:
        problems.append("CI lane does not reject skip / xfail / deselect")
    return problems


# --- AC1: source parity, pins, frontmatter and boundaries --------------------------------


def test_ac1_files_reproduce_the_pinned_source(manifest: dict[str, Any]) -> None:
    assert parity_problems(manifest, ROOT) == []
    assert issue_pin_problems(manifest) == []
    assert len(manifest["documents"]) == 2 and len(manifest["verbatim_files"]) == 10


def test_ac1_references_are_exactly_the_loading_map_set(manifest: dict[str, Any]) -> None:
    assert reference_map_problems(manifest, ROOT) == []


def test_ac1_skill_is_disabled_and_the_lite_agent_stays_read_only(manifest: dict[str, Any]) -> None:
    assert frontmatter_problems(manifest, ROOT) == []


def test_ac1_lite_deny_list_covers_the_python_repo_layout(manifest: dict[str, Any]) -> None:
    assert deny_list_problems(manifest, ROOT) == []


def test_ac1_lite_lines_changed_are_recorded_as_intentional_diff(manifest: dict[str, Any]) -> None:
    diffs = manifest["documents"][LITE]["intentional_diff"]
    joined = "\n".join(e["moved"] for e in diffs)
    assert "scripts/**" in joined and "uv.lock" in joined and "agent-platform" in joined


# --- AC2: the unchanged source test runs for real; counts are measured -------------------


def test_ac2_the_restored_consumer_test_passes_for_real() -> None:
    r = _pytest(RESTORED_ID)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 passed" in out, out


def test_ac2_source_tests_run_108_with_nothing_deferred(manifest: dict[str, Any]) -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/run_stage1b1_tests.py", D5_MANIFEST_REL],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "108 collected, 0 deferred with reasons, 108 ran, 0 skipped" in out, out
    assert "108 passed" in out and "deselected" not in out, out
    assert not any(w in out for w in ("xfailed", "xpassed", "skipped,")), out


def test_ac2_the_three_consumers_share_the_canonical_evaluator() -> None:
    assert consumer_problems(ROOT) == []


def test_ac2_d4_deferral_is_released_and_history_is_kept(
    manifest: dict[str, Any], d4_manifest: dict[str, Any]
) -> None:
    assert restoration_problems(manifest, d4_manifest, ROOT) == []


# --- AC3: boundaries, unwired dependencies, CI lane --------------------------------------


def test_ac3_unmigrated_hard_dependencies_have_owner_and_stay_absent(
    manifest: dict[str, Any],
) -> None:
    assert dependency_problems(manifest, ROOT) == []


def test_ac3_evidence_boundary_is_not_overclaimed(manifest: dict[str, Any]) -> None:
    assert boundary_problems(manifest) == []


def test_ac3_skill_text_does_not_claim_production_operation() -> None:
    body = (ROOT / SKILL).read_text(encoding="utf-8").split("---", 2)[2]
    note = body.split("## Input", 1)[0]
    assert "preload" in note and "実行しない" in note and "fail-closed" in note
    assert "本番稼働" in note  # states that the deferred state is not proof of production use


def test_ac3_d5_ci_lane_pins_the_manifest_counts(manifest: dict[str, Any]) -> None:
    assert lane_problems(CI.read_text(encoding="utf-8"), manifest) == []
    assert d4.lane_problems(CI.read_text(encoding="utf-8"), _json(D4_MANIFEST)) == []


def test_ac3_the_closure_test_count_is_measured_not_assumed(manifest: dict[str, Any]) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", THIS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    ids = [ln for ln in r.stdout.splitlines() if "::" in ln]
    assert r.returncode == 0 and ids, r.stdout + r.stderr
    assert manifest["lane_totals"]["closure_tests_ran"] == len(ids)
    assert manifest["lane_totals"]["source_ran"] == manifest["totals"]["ran"]


# --- AC4: stale assertions resolved; negative controls -----------------------------------


def test_ac4_no_earlier_slice_asserts_the_d5_files_are_absent() -> None:
    assert stale_absence_problems(ROOT) == []


def test_ac4_stale_absence_assertion_is_detected(tmp_path: Path) -> None:
    tdir = tmp_path / "tests" / "stage_i4"
    tdir.mkdir(parents=True)
    for p in (ROOT / "tests" / "stage_i4").glob("test_i4_4d[1-4]_*.py"):
        shutil.copy2(p, tdir / p.name)
    assert stale_absence_problems(tmp_path) == []
    d3 = tdir / "test_i4_4d3_step4_step5.py"
    d3.write_text(
        d3.read_text() + '\n    assert not (ROOT / "pr-review-judge" / "SKILL.md").exists()\n',
        encoding="utf-8",
    )
    assert any("stale absence" in p for p in stale_absence_problems(tmp_path))


def test_ac4_source_sha_and_document_tampering_are_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    assert any("missing" in p for p in parity_problems(manifest, tmp_path))
    _copy_tree(manifest, tmp_path)
    assert parity_problems(manifest, tmp_path) == []
    ref = tmp_path / f"{SK}/references/evidence-policy.md"
    ref.write_text(ref.read_text() + "\nweakened\n", encoding="utf-8")
    assert any("moved_sha256 mismatch" in p for p in parity_problems(manifest, tmp_path))
    wrong = copy.deepcopy(manifest)
    wrong["documents"][SKILL]["source_blob_sha1"] = "0" * 40
    assert any("source blob SHA" in p for p in parity_problems(wrong, ROOT))
    assert any("Issue-fixed pin" in p for p in issue_pin_problems(wrong))
    hollow = copy.deepcopy(manifest)
    hollow["documents"][LITE]["intentional_diff"] = []
    assert any("intentional_diff not recorded" in p for p in parity_problems(hollow, ROOT))
    edited = tmp_path / SKILL
    edited.write_text(edited.read_text() + "\nextra\n", encoding="utf-8")
    assert any("moved_sha256 mismatch" in p for p in parity_problems(manifest, tmp_path))


def test_ac4_a_residual_deferral_or_count_tampering_is_detected(
    manifest: dict[str, Any], d4_manifest: dict[str, Any]
) -> None:
    residual = copy.deepcopy(d4_manifest)
    residual["deferred_tests"] = [{"id": RESTORED_ID}]
    assert any("still recorded" in p for p in restoration_problems(manifest, residual, ROOT))
    for key, val in (("ran", 107), ("deferred", 1), ("source_collected", 109)):
        bad = copy.deepcopy(d4_manifest)
        bad["totals"][key] = val
        assert any("totals are not 108" in p for p in restoration_problems(manifest, bad, ROOT))
    bad = copy.deepcopy(d4_manifest)
    bad["expected_ran"][SOURCE_TEST] = 69
    assert restoration_problems(manifest, bad, ROOT)
    lost = copy.deepcopy(d4_manifest)
    del lost["historical_pre_d5"]
    assert any("historical_pre_d5" in p for p in restoration_problems(manifest, lost, ROOT))
    untracked = copy.deepcopy(d4_manifest)
    untracked["restored_by_d5"]["id"] = "other"
    assert any("restored_by_d5" in p for p in restoration_problems(manifest, untracked, ROOT))


def test_ac4_a_weakened_source_test_is_detected(
    manifest: dict[str, Any], d4_manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    d4._copy_tree(d4_manifest, tmp_path)
    assert restoration_problems(manifest, d4_manifest, tmp_path) == []
    test_file = tmp_path / SOURCE_TEST
    text = test_file.read_text(encoding="utf-8")
    test_file.write_text(
        text.replace(
            '"pr-reviewer-lite.md": REPO_ROOT / ".claude" / "agents" / "pr-reviewer-lite.md",', ""
        ),
        encoding="utf-8",
    )
    problems = restoration_problems(manifest, d4_manifest, tmp_path)
    assert any("lost its assertion" in p for p in problems)
    assert any("source parity" in p for p in problems)
    test_file.write_text(text + "\n@pytest.mark.skip\ndef test_x():\n    pass\n", encoding="utf-8")
    assert any(
        "skip / xfail marker" in p for p in restoration_problems(manifest, d4_manifest, tmp_path)
    )
    gone = text.replace("assert path_obj.is_file()", "pass  # weakened")
    test_file.write_text(gone, encoding="utf-8")
    assert any(
        "lost its assertion" in p for p in restoration_problems(manifest, d4_manifest, tmp_path)
    )


def test_ac4_a_missing_consumer_or_dropped_reference_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    assert consumer_problems(tmp_path) == []
    lite = tmp_path / LITE
    lite.write_text(lite.read_text().replace("wait_ci_checks", "other_evaluator"), encoding="utf-8")
    assert any("does not reference wait_ci_checks" in p for p in consumer_problems(tmp_path))
    lite.unlink()
    assert any("consumer missing" in p for p in consumer_problems(tmp_path))
    assert reference_map_problems(manifest, tmp_path) == []
    (tmp_path / SK / "references" / "best-practices.md").write_text("stub\n", encoding="utf-8")
    assert any("unrecorded reference" in p for p in reference_map_problems(manifest, tmp_path))
    skill = tmp_path / SKILL
    skill.write_text(skill.read_text().replace("deterministic-gates.md", "x.md"), encoding="utf-8")
    assert any("no longer names" in p for p in reference_map_problems(manifest, tmp_path))


def test_ac4_re_enabling_the_skill_or_widening_the_lite_agent_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    assert frontmatter_problems(manifest, tmp_path) == []
    skill = tmp_path / SKILL
    original = skill.read_text(encoding="utf-8")
    skill.write_text(
        original.replace("disable-model-invocation: true", "disable-model-invocation: false"),
        encoding="utf-8",
    )
    assert any("disable-model-invocation" in p for p in frontmatter_problems(manifest, tmp_path))
    skill.write_text(original.replace("disable-model-invocation: true\n", ""), encoding="utf-8")
    assert any("disable-model-invocation" in p for p in frontmatter_problems(manifest, tmp_path))
    skill.write_text(original.replace("preload", "load"), encoding="utf-8")
    assert any("fail-closed explanation" in p for p in frontmatter_problems(manifest, tmp_path))
    skill.write_text(original, encoding="utf-8")
    lite = tmp_path / LITE
    base = lite.read_text(encoding="utf-8")
    for old, new, msg in (
        ("tools:\n  - Bash", "tools:\n  - Write\n  - Bash", "editing tool"),
        ("  - Skill\npermissionMode", "permissionMode", "disallowedTools"),
        ("permissionMode: dontAsk", "permissionMode: acceptEdits", "permissionMode"),
    ):
        assert old in base, old
        lite.write_text(base.replace(old, new, 1), encoding="utf-8")
        assert any(msg in p for p in frontmatter_problems(manifest, tmp_path)), msg
    lite.write_text(base, encoding="utf-8")
    assert deny_list_problems(manifest, tmp_path) == []
    lite.write_text(base.replace('    - "scripts/**"', '    - "docs-only/**"'), encoding="utf-8")
    assert any("scripts/**" in p for p in deny_list_problems(manifest, tmp_path))
    lite.write_text(base.replace('    - "uv.lock"', '    - "pnpm-lock.yaml"'), encoding="utf-8")
    assert any("uv.lock" in p for p in deny_list_problems(manifest, tmp_path))


def test_ac4_dependency_and_boundary_tampering_are_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    ownerless = copy.deepcopy(manifest)
    ownerless["unmigrated_references"]["hard_dependencies"][0]["owner"] = ""
    assert any("without owner" in p for p in dependency_problems(ownerless, ROOT))
    forgotten = copy.deepcopy(manifest)
    forgotten["unmigrated_references"]["hard_dependencies"] = forgotten["unmigrated_references"][
        "hard_dependencies"
    ][:1]
    assert any("not recorded" in p for p in dependency_problems(forgotten, ROOT))
    for rel in (f"{SK}/scripts/ci_verdict_summary_v2.py", ".claude/hooks/pr_reviewer_guard.py"):
        fake = tmp_path / rel
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("# empty stub\n", encoding="utf-8")
    assert any("present" in p for p in dependency_problems(manifest, tmp_path))
    over = copy.deepcopy(manifest)
    over["evidence_boundary"]["skill_preload_effective"] = True
    assert any("overclaims" in p for p in boundary_problems(over))
    over = copy.deepcopy(manifest)
    over["evidence_boundary"]["agent_dispatched"] = True
    assert any("overclaims" in p for p in boundary_problems(over))


def test_ac4_ci_lane_tampering_is_detected(manifest: dict[str, Any]) -> None:
    text = CI.read_text(encoding="utf-8")
    assert lane_problems(text, manifest) == []
    assert d4.lane_problems(text, _json(D4_MANIFEST)) == []
    assert any(
        "ran count" in p
        for p in lane_problems(
            text.replace("0 deferred with reasons, 108 ran", "0 deferred with reasons, 107 ran"),
            manifest,
        )
    )
    assert any(
        "does not run its manifest" in p
        for p in lane_problems(
            text.replace("i4-4d5-pr-review-consumers-manifest.json | tee", "other.json | tee"),
            manifest,
        )
    )
    assert any(
        "reject skip" in p
        for p in lane_problems(
            text.replace("(skipped|xfailed|xpassed|deselected)", "(skipped)"), manifest
        )
    )
    assert any(
        "CI lane missing" in p for p in lane_problems(text.replace(D5_JOB, "x-job"), manifest)
    )
    assert d4.lane_problems(
        text.replace("0 deferred with reasons, 108 ran", "1 deferred with reasons, 107 ran"),
        _json(D4_MANIFEST),
    )


def test_ac4_historical_manifests_are_not_rewritten(
    manifest: dict[str, Any], d4_manifest: dict[str, Any]
) -> None:
    d3 = _json(DEV / "i4-4d3-step4-step5-manifest.json")
    assert "note_i_4_4d5" in d3["unmigrated_references"]
    assert any(
        "pr-review-judge" in e["reference"]
        for e in d3["unmigrated_references"]["later_i_4_4d_slices"]
    )
    assert any(
        "pr-review-judge" in e["reference"]
        for e in d4_manifest["unmigrated_references"]["migrated_by_d5"]
    )
    c2 = _json(DEV / "i4-4c2-intake-capsule-manifest.json")
    assert (c2["totals"]["ran"], c2["totals"]["deferred"]) == (118, 1)
