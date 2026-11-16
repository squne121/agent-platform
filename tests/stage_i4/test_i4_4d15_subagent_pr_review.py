"""I-4-4D15 (#115): the limited, main-session-dispatched read-only `pr-reviewer` SubAgent wiring.

What this module proves (static, no GitHub, no model call):
* the `pr-review-judge` Skill is released from ``disable-model-invocation`` for D15 only, and the
  other disabled Skills (`impl-review-loop`, `open-pr`) stay disabled;
* the Skill carries a "Dispatched Read-only Review (D15)" section that reuses the D13 M1->M8
  procedure unchanged, returns the four-field result and forbids every GitHub write;
* the `pr-reviewer` Agent keeps its read-only role (tools / disallowedTools / no hook), preloads the
  Skill and its text does not contradict the D15 path (historical "no dispatch" wording is explicitly
  reconciled instead of silently kept);
* the earlier slices' current-state records (D4 / D5 / D8) were synchronized while their source pins
  and historical records were kept, and the CI lane is wired.

What it does NOT prove: that a real `pr-reviewer` SubAgent was dispatched and preloaded the Skill.
That runtime acceptance (AC4) is observed by the main session against the real Draft PR and is
recorded outside this file; a fixture / static result here is never a runtime PASS.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d15-subagent-pr-review-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
D4_MANIFEST = DEV / "i4-4d4-skill-agents-readonly-manifest.json"
D5_MANIFEST = DEV / "i4-4d5-pr-review-consumers-manifest.json"
D8_MANIFEST = DEV / "i4-4d8-hookless-reviewer-manifest.json"
THIS = "tests/stage_i4/test_i4_4d15_subagent_pr_review.py"
D15_JOB = "i4-4d15-subagent-pr-review"
D15_MANIFEST_REL = "docs/dev/i4-4d15-subagent-pr-review-manifest.json"
SKILL = ".claude/skills/pr-review-judge/SKILL.md"
AGENT = ".claude/agents/pr-reviewer.md"
D15_SECTION = "## Dispatched Read-only Review (D15)"
D13_SECTION = "## Manual Read-only Review (D13)"
STILL_DISABLED = (
    ".claude/skills/impl-review-loop/SKILL.md",
    ".claude/skills/open-pr/SKILL.md",
)
GH_WRITE = re.compile(
    r"gh\s+pr\s+(comment|review|edit|create|merge|ready|close|reopen|update-branch)\b"
    r"|gh\s+issue\s+(comment|edit|close|create)\b|git\s+(push|commit|merge|rebase|reset)\b"
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


d2 = _load("tests/stage_i4/test_i4_4d2_step2_verification.py", "d2_helpers_for_i4_4d15")
job_runs = d2.job_runs


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


def frontmatter(text: str) -> dict[str, Any]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match
    loaded = yaml.safe_load(match.group(1))
    assert isinstance(loaded, dict)
    return loaded


def skill_text() -> str:
    return (ROOT / SKILL).read_text(encoding="utf-8")


def agent_text() -> str:
    return (ROOT / AGENT).read_text(encoding="utf-8")


def d15_section(text: str) -> str:
    if D15_SECTION not in text:
        return ""
    return text.split(D15_SECTION, 1)[1].split("\n## ", 1)[0]


# --- problem functions (pure: they return a list of messages, [] means OK) ---------------------


def skill_problems(text: str) -> list[str]:
    problems: list[str] = []
    fm = frontmatter(text)
    if fm.get("name") != "pr-review-judge":
        problems.append("Skill name changed")
    if "disable-model-invocation" in fm:
        problems.append("disable-model-invocation must stay released for D15 (key present)")
    section = d15_section(text)
    if not section:
        return [*problems, "Dispatched Read-only Review (D15) section is missing"]
    for needle in ("main session", "明示", "pr-reviewer", "M1→M8", "verdict", "reviewed_head_sha"):
        if needle not in section:
            problems.append(f"D15 section lost: {needle}")
    for needle in ("blockers", "warnings", "insufficient_context", "terminal completion"):
        if needle not in section:
            problems.append(f"D15 section lost: {needle}")
    if "自動 invoke することが技術的に可能" not in section:
        problems.append("D15 section hides the side effect of releasing the disable")
    if "impl-review-loop" not in section or "Final Gate" not in section:
        problems.append("D15 section no longer keeps the loop / Final Gate unapproved")
    if "runtime evidence として数えない" not in section:
        problems.append("D15 section no longer excludes the Reviewer's own output as evidence")
    if GH_WRITE.search(section.replace("`gh pr review`", "")):
        problems.append("D15 section contains a GitHub-write / git mutation command")
    if D13_SECTION not in text or "M1→M8" not in text.split(D15_SECTION, 1)[1]:
        problems.append("D13 manual procedure reference lost")
    body_head = text.split("---", 2)[2].split(D13_SECTION, 1)[0]
    if "I-4-4D15" not in body_head:
        problems.append("migration note lost the D15 current-state line")
    d13 = text.split(D13_SECTION, 1)[1].split("\n## ", 1)[0]
    for line in d13.splitlines():
        if "dispatch も preload もしない" in line and "D15 (#115) の現行状態" not in line:
            problems.append("D13 no-dispatch sentence is not reconciled with D15")
    return problems


def agent_problems(text: str) -> list[str]:
    problems: list[str] = []
    fm = frontmatter(text)
    if fm.get("name") != "pr-reviewer":
        problems.append("Agent name changed")
    if fm.get("skills") != ["pr-review-judge"]:
        problems.append("Agent no longer preloads pr-review-judge")
    if fm.get("tools") != ["Bash", "Read", "Grep", "Glob"]:
        problems.append("Agent tools changed")
    if fm.get("disallowedTools") != ["Edit", "Write", "MultiEdit"]:
        problems.append("Agent disallowedTools changed")
    if "hooks" in fm:
        problems.append("Agent gained a hook")
    if fm.get("permissionMode") != "dontAsk":
        problems.append("Agent permissionMode changed")
    body = text.split("---", 2)[2]
    if "D15" not in body or "Dispatched Read-only Review (D15)" not in body:
        problems.append("Agent lost the D15 dispatch note")
    for needle in (
        "main session",
        "単一 PR",
        "verdict",
        "reviewed_head_sha",
        "blockers",
        "warnings",
    ):
        if needle not in body:
            problems.append(f"Agent D15 note lost: {needle}")
    if "終端してから完了を報告する" not in body:
        problems.append("Agent no longer forbids claiming completion before termination")
    if "実 dispatch しない" in body and "D4/D8 時点の履歴記述" not in body:
        problems.append("Agent keeps the D4/D8 no-dispatch wording without reconciling it")
    return problems


def disable_state_problems(root: Path) -> list[str]:
    problems: list[str] = []
    for rel in STILL_DISABLED:
        fm = frontmatter((root / rel).read_text(encoding="utf-8"))
        if fm.get("disable-model-invocation") is not True:
            problems.append(f"disable-model-invocation was released: {rel}")
    for path in sorted((root / ".claude/skills").glob("*/SKILL.md")):
        rel = path.relative_to(root).as_posix()
        if rel in STILL_DISABLED or rel == SKILL:
            continue
        if frontmatter(path.read_text(encoding="utf-8")).get("disable-model-invocation"):
            problems.append(f"unexpected extra disabled Skill: {rel}")
    return problems


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    runs = job_runs(ci_text, D15_JOB)
    if runs is None:
        return [f"CI lane missing: {D15_JOB}"]
    problems: list[str] = []
    if f"run_stage1b1_tests.py {D15_MANIFEST_REL}" not in runs:
        problems.append("CI lane does not run its manifest")
    t = m["totals"]
    if f"{t['deferred']} deferred with reasons, {t['ran']} ran, 0 skipped" not in runs:
        problems.append("CI lane does not pin the manifest ran count")
    if "tests/stage_i4" not in runs:
        problems.append("CI lane does not run the closure tests")
    if "(skipped|xfailed|xpassed|deselected)" not in runs:
        problems.append("CI lane does not reject skip / xfail / deselect")
    return problems


def boundary_problems(m: dict[str, Any]) -> list[str]:
    b = m["evidence_boundary"]
    problems = [f"{key} must be false" for key in m["boundary_false"] if b.get(key) is not False]
    if b.get("subagent_runtime_acceptance") != "OBSERVED_BY_MAIN_SESSION_OUTSIDE_THIS_FILE":
        problems.append("the SubAgent runtime acceptance must stay a main-session observation")
    if b.get("fixture_result_is_runtime_pass") is not False:
        problems.append("a static result must not be recorded as runtime PASS")
    return problems


# --- AC1: the Skill release and the Agent role ---------------------------------------------------


def test_ac1_skill_is_released_for_d15_only_and_declares_the_dispatched_review() -> None:
    assert skill_problems(skill_text()) == []


def test_ac1_only_pr_review_judge_is_released_and_other_skills_stay_disabled() -> None:
    assert disable_state_problems(ROOT) == []


def test_ac1_agent_keeps_the_read_only_role_and_preloads_the_skill() -> None:
    assert agent_problems(agent_text()) == []


def test_ac1_no_hook_settings_wrapper_or_new_script_was_added() -> None:
    assert not (ROOT / ".claude/hooks").exists()
    assert not (ROOT / ".claude/settings.json").exists()
    assert not list((ROOT / ".claude/skills/pr-review-judge").glob("*.py"))
    for forbidden in ("REVIEWER_DISPATCH", "permission-wrapper", "dispatch_reviewer"):
        assert forbidden not in skill_text() + agent_text()


# --- AC2: the earlier slices stay consistent and their history is kept -------------------------


def test_ac2_current_hashes_are_synchronized_and_history_is_kept() -> None:
    d4, d5, d8 = _json(D4_MANIFEST), _json(D5_MANIFEST), _json(D8_MANIFEST)
    skill = hashlib.sha256((ROOT / SKILL).read_bytes()).hexdigest()
    agent = hashlib.sha256((ROOT / AGENT).read_bytes()).hexdigest()
    assert (
        d5["documents"][SKILL]["moved_sha256"]
        == skill
        == d8["documents"][SKILL]["current_moved_sha256"]
    )
    assert (
        d4["documents"][AGENT]["moved_sha256"]
        == agent
        == d8["documents"][AGENT]["current_moved_sha256"]
    )
    for rec in (d5["documents"][SKILL], d4["documents"][AGENT], d8["documents"][SKILL]):
        assert "historical_pre_d15" in rec
    assert d5["documents"][SKILL]["historical_pre_d15"]["moved_sha256"] != skill
    assert d4["documents"][AGENT]["historical_pre_d15"]["moved_sha256"] != agent
    assert d5["documents"][SKILL]["source_blob_sha1"] == "db2ae9d1f17498fa358528f552974f7e2666c8fa"
    assert len(d4["documents"][AGENT]["intentional_diff"]) == 3


def test_ac2_d15_dispatch_wording_is_recorded_as_intentional_diff() -> None:
    d5, d4 = _json(D5_MANIFEST), _json(D4_MANIFEST)
    skill_diffs = d5["documents"][SKILL]["intentional_diff"]
    assert any(d15_section(skill_text()) and "D15" in e["what"] for e in skill_diffs)
    assert "D15" in d4["documents"][AGENT]["intentional_diff"][2]["moved"]
    # the released frontmatter key is kept as history, not as a current edit
    hist = d5["documents"][SKILL]["historical_pre_d15"]
    assert hist["intentional_diff_0"]["moved"] == "disable-model-invocation: true\n"


# --- AC3: manifest, boundary, CI lane ----------------------------------------------------------


def test_ac3_manifest_pins_files_counts_and_the_ci_lane(manifest: dict[str, Any]) -> None:
    for rel in manifest["required_files"]:
        assert (ROOT / rel).is_file(), rel
    assert THIS in manifest["test_files"]
    assert sum(manifest["expected_ran"].values()) == manifest["totals"]["ran"]
    assert manifest["expected_collect"] == manifest["expected_ran"]
    assert manifest["deferred_tests"] == [] and manifest["totals"]["skipped"] == 0
    assert lane_problems(CI.read_text(encoding="utf-8"), manifest) == []


def test_ac3_evidence_boundary_is_not_overclaimed(manifest: dict[str, Any]) -> None:
    assert boundary_problems(manifest) == []
    assert manifest["approved_scope"]["not_approved"]


def test_ac3_the_module_adds_no_skip_xfail_or_deselect() -> None:
    text = Path(__file__).read_text(encoding="utf-8")
    decorator = "@pytest" + ".mark."
    call = "pytest" + "."
    for needle in (decorator + "skip", decorator + "xfail", call + "skip(", call + "xfail("):
        assert needle not in text


# --- negative controls: each regression is detected -------------------------------------------


def _skill_mutated(old: str, new: str) -> list[str]:
    text = skill_text()
    assert old in text, old
    return skill_problems(text.replace(old, new))


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        (
            "name: pr-review-judge\n",
            "name: pr-review-judge\ndisable-model-invocation: true\n",
            "disable-model-invocation",
        ),
        (
            "name: pr-review-judge\n",
            "name: pr-review-judge\ndisable-model-invocation: false\n",
            "disable-model-invocation",
        ),
        (D15_SECTION, "## Something Else", "section is missing"),
        ("M1→M8", "M1", "M1→M8"),
        ("terminal completion", "completion", "terminal completion"),
        ("insufficient_context", "stop", "insufficient_context"),
        ("自動 invoke することが技術的に可能", "自動 invoke は不可能", "hides the side effect"),
        ("Final Gate", "Gate", "Final Gate"),
        ("runtime evidence として数えない", "証拠として数える", "own output"),
        ("GitHub への verdict 投稿、`gh pr review`、", "`gh pr merge 1`、", "GitHub-write"),
        ("D15 (#115) の現行状態", "旧状態", "not reconciled"),
    ],
)
def test_neg_skill_regressions_are_detected(old: str, new: str, expect: str) -> None:
    assert any(expect in p for p in _skill_mutated(old, new)), expect


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("  - pr-review-judge\n---", "  - other\n---", "preloads"),
        ("tools:\n  - Bash\n", "tools:\n  - Write\n  - Bash\n", "tools changed"),
        ("disallowedTools:\n  - Edit\n", "disallowedTools:\n", "disallowedTools"),
        ("permissionMode: dontAsk", "permissionMode: bypassPermissions", "permissionMode"),
        ("skills:\n", "hooks:\n  PreToolUse: []\nskills:\n", "hook"),
        ("D4/D8 時点の履歴記述", "履歴", "reconciling"),
        ("verdict", "v", "Agent D15 note lost"),
    ],
)
def test_neg_agent_regressions_are_detected(old: str, new: str, expect: str) -> None:
    text = agent_text()
    assert old in text, old
    assert any(expect in p for p in agent_problems(text.replace(old, new))), expect


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("skills:\n  - pr-review-judge\n", "skills: pr-review-judge\n", "preloads"),
        ("skills:\n  - pr-review-judge\n", "skills: []\n", "preloads"),
        ("終端してから完了を報告する", "完了を報告する", "before termination"),
    ],
)
def test_neg_preload_type_and_premature_completion_are_detected(
    old: str, new: str, expect: str
) -> None:
    text = agent_text()
    assert old in text, old
    assert any(expect in p for p in agent_problems(text.replace(old, new))), expect


@pytest.mark.parametrize("field", ["verdict", "reviewed_head_sha", "blockers", "warnings"])
def test_neg_a_missing_return_field_in_the_dispatched_section_is_detected(field: str) -> None:
    text = skill_text()
    head, section = text.split(D15_SECTION, 1)
    body, rest = (section.split("\n## ", 1) + [""])[:2]
    mutated = head + D15_SECTION + body.replace(field, "x") + ("\n## " + rest if rest else "")
    assert any(field in p for p in skill_problems(mutated)), field


def test_neg_releasing_another_disabled_skill_is_detected(tmp_path: Path) -> None:
    for rel in (SKILL, *STILL_DISABLED):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    assert disable_state_problems(tmp_path) == []
    open_pr = tmp_path / STILL_DISABLED[1]
    original = open_pr.read_text(encoding="utf-8")
    open_pr.write_text(
        original.replace("disable-model-invocation: true\n", "", 1), encoding="utf-8"
    )
    assert any("was released" in p for p in disable_state_problems(tmp_path))
    open_pr.write_text(original, encoding="utf-8")
    extra = tmp_path / ".claude/skills/other/SKILL.md"
    extra.parent.mkdir(parents=True)
    extra.write_text("---\nname: other\ndisable-model-invocation: true\n---\nx\n", encoding="utf-8")
    assert any("extra disabled Skill" in p for p in disable_state_problems(tmp_path))


def test_neg_lane_and_boundary_regressions_are_detected(manifest: dict[str, Any]) -> None:
    ci = CI.read_text(encoding="utf-8")
    assert any("missing" in p for p in lane_problems(ci.replace(D15_JOB, "x"), manifest))
    assert any(
        "ran count" in p for p in lane_problems(ci.replace(" 0 skipped", " 1 skipped"), manifest)
    )
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["verdict_published"] = True
    assert any("verdict_published" in p for p in boundary_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["fixture_result_is_runtime_pass"] = True
    assert any("runtime PASS" in p for p in boundary_problems(bad))
