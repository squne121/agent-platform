"""I-4-4D13 (#111): the explicit manual read-only PR review path of pr-review-judge.

What this module proves: the Skill text declares one ordered, read-only procedure that reuses the
existing helper CLIs, and those helper CLIs (run for real against isolated fixtures) answer
"not OK" for malformed / stale / foreign / unknown evidence, which the Skill maps to blockers.

What it does NOT prove: that a real interactive `/pr-review-judge <PR>` ran. That live runtime
acceptance (AC4) is recorded by a human-invoked run and stays UNVERIFIED until then; a fixture
result here is never a runtime PASS. No Reviewer SubAgent is dispatched, nothing is published.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d13-manual-pr-review-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
D5_MANIFEST = DEV / "i4-4d5-pr-review-consumers-manifest.json"
D8_MANIFEST = DEV / "i4-4d8-hookless-reviewer-manifest.json"
THIS = "tests/stage_i4/test_i4_4d13_manual_pr_review.py"
D13_JOB = "i4-4d13-manual-pr-review"
D13_MANIFEST_REL = "docs/dev/i4-4d13-manual-pr-review-manifest.json"
SK = ".claude/skills/pr-review-judge"
SKILL = f"{SK}/SKILL.md"
SCRIPTS = ROOT / SK / "scripts"
EVALUATOR = ".claude/skills/open-pr/scripts/validate_pr_body.py"
WAIT_CI = ".claude/skills/impl-review-loop/scripts/wait_ci_checks.py"
STEP_IDS = [f"M{i}" for i in range(1, 9)]
SECTION = "## Manual Read-only Review (D13)"
# Scripts that existed before D13 (no new judge / schema / wrapper may appear).
EXPECTED_SCRIPTS = {
    "allowed_paths_review_gate.py",
    "check_pr_review_gates.py",
    "ci_verdict_summary.py",
    "tests",
}
MUTATING = re.compile(
    r"gh (?:pr|issue) (?:comment|edit|review|create|merge|ready|close|reopen|update-branch)"
    r"|git (?:push|commit|merge|rebase|reset)"
    r"|gh api[^`]*(?:-X|--method)[ =]*(?:POST|PATCH|PUT|DELETE)"
    r"|gh api[^`]* -[fF] "
    r"|gh api[^`]*--input",
    re.IGNORECASE,
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


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


def frontmatter(text: str) -> dict[str, Any]:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1]) or {}


# --- problem functions (pure: they return messages, [] means OK) -------------------------------


def manual_section(text: str) -> str:
    if SECTION not in text:
        return ""
    return text.split(SECTION, 1)[1].split("\n## ", 1)[0]


def split_section(section: str) -> tuple[str, dict[str, str], str]:
    """prohibition paragraph, {M1..M8: step text}, and the verdict / output paragraphs."""
    head, _, tail = section.partition("**手順")
    prohibition = head
    steps_part, _, rest = tail.partition("**verdict**")
    steps: dict[str, str] = {}
    pieces = re.split(r"(?m)^\d+\. \*\*(M\d) ", steps_part)
    for i in range(1, len(pieces) - 1, 2):
        steps[pieces[i]] = pieces[i + 1]
    return prohibition, steps, rest


def skill_problems(text: str) -> list[str]:
    problems: list[str] = []
    fm = frontmatter(text)
    if fm.get("name") != "pr-review-judge":
        problems.append("Skill name changed")
    if fm.get("disable-model-invocation") is not True:
        problems.append("disable-model-invocation: true must stay")
    section = manual_section(text)
    if not section:
        return [*problems, "Manual Read-only Review (D13) section is missing"]
    prohibition, steps, rest = split_section(section)
    for needle in ("ユーザー自身", "明示的", "Claude が自分で", "preload"):
        if needle not in section:
            problems.append(f"manual-only trigger statement lost: {needle}")
    if list(steps) != STEP_IDS:
        problems.append(f"procedure steps are not exactly M1..M8 in order: {list(steps)}")
    # the helper CLIs are called in the declared order
    order = [
        ("M1", "gh repo view"),
        ("M1", "headRefOid"),
        ("M2", "gh issue view"),
        ("M3", "validate_pr_body.py"),
        ("M3", "--evaluate-reference-policy"),
        ("M4", "allowed_paths_review_gate.py"),
        ("M5", "wait_ci_checks.py"),
        ("M5", "ci_verdict_summary.py"),
        ("M6", "check_pr_review_gates.py"),
        ("M7", "gh pr diff"),
        ("M8", "headRefOid"),
    ]
    for step, needle in order:
        if needle not in steps.get(step, ""):
            problems.append(f"{step} no longer names {needle}")
    for status in (
        "pending_timeout",
        "no_checks",
        "skipped_only",
        "failed",
        "cancelled",
        "head_sha_changed",
        "gh_error",
    ):
        if status not in steps.get("M5", ""):
            problems.append(f"M5 does not treat CI status {status} as not-passed")
    if "== passed" not in steps.get("M5", "") or "current_head_sha" not in steps.get("M5", ""):
        problems.append("M5 must require status passed on the current head")
    for needle in ("fail_closed", "foreign", "stale", "indeterminate"):
        if needle not in "".join(steps.values()):
            problems.append(f"procedure lost the fail-closed marker {needle}")
    # read-only: no mutating command may appear outside the prohibition paragraph
    for step, body in {**steps, "verdict/output": rest}.items():
        if MUTATING.search(body):
            problems.append(f"{step} contains a GitHub / repository mutating command")
    for needle in ("gh pr comment|edit|review", "git push", "gh api"):
        if needle not in prohibition:
            problems.append(f"prohibition list lost {needle}")
    if "6) verdict 投稿" not in prohibition:
        problems.append("the verdict publication step is no longer excluded")
    if "OS／権限による隔離ではない" not in section:
        problems.append("the read-only rule is not labeled an operating instruction")
    if "技術的に拒否されるとは主張せず" not in section:
        problems.append("the no technical-denial claim was dropped")
    # verdict ladder
    if (
        "APPROVE" not in rest
        or "HUMAN_REVIEW_REQUIRED" not in rest
        or "REQUEST_CHANGES" not in rest
    ):
        problems.append("verdict ladder lost a value")
    for needle in ("blocker が 0 件", "M8 で head が不変", "stale", "foreign"):
        if needle not in rest:
            problems.append(f"APPROVE precondition lost: {needle}")
    for field in ("verdict", "reviewed_head_sha", "blockers", "warnings"):
        if field not in rest:
            problems.append(f"output contract lost {field}")
    if "新しい schema field・wrapper・script は追加しない" not in rest:
        problems.append("the no-new-schema/wrapper statement was dropped")
    # no dispatch
    if re.search(r"Agent\(|subagent_type|Task\(", section):
        problems.append("the manual path references a SubAgent dispatch call")
    return problems


def scripts_problems(root: Path) -> list[str]:
    present = {p.name for p in (root / SK / "scripts").iterdir() if p.name != "__pycache__"}
    extra = sorted(present - EXPECTED_SCRIPTS)
    return [f"new script under pr-review-judge/scripts: {extra}"] if extra else []


def boundary_problems(m: dict[str, Any]) -> list[str]:
    b = m["evidence_boundary"]
    problems = [f"{key} must be false" for key in m["boundary_false"] if b.get(key) is not False]
    if b.get("manual_skill_runtime") != "UNVERIFIED_UNTIL_HUMAN_INVOKE":
        problems.append("the live manual Skill runtime must stay UNVERIFIED in the fixture lane")
    if b.get("fixture_result_is_runtime_pass") is not False:
        problems.append("a fixture result must not be recorded as runtime PASS")
    return problems


def job_runs(ci_text: str, job: str) -> str | None:
    jobs = yaml.safe_load(ci_text)["jobs"]
    if job not in jobs:
        return None
    return "\n".join(s.get("run", "") for s in jobs[job].get("steps", []))


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    runs = job_runs(ci_text, D13_JOB)
    if runs is None:
        return [f"CI lane missing: {D13_JOB}"]
    problems: list[str] = []
    if f"run_stage1b1_tests.py {D13_MANIFEST_REL}" not in runs:
        problems.append("CI lane does not run its manifest")
    t = m["totals"]
    if f"{t['deferred']} deferred with reasons, {t['ran']} ran, 0 skipped" not in runs:
        problems.append("CI lane does not pin the manifest ran count")
    if "tests/stage_i4" not in runs:
        problems.append("CI lane does not run the closure tests")
    if "(skipped|xfailed|xpassed|deselected)" not in runs:
        problems.append("CI lane does not reject skip / xfail / deselect")
    return problems


def history_problems(d5: dict[str, Any], d8: dict[str, Any]) -> list[str]:
    """D5 / D8 keep their source pins and earlier records next to the D13 current state."""
    problems: list[str] = []
    rec5 = d5["documents"][SKILL]
    if rec5["source_blob_sha1"] != "db2ae9d1f17498fa358528f552974f7e2666c8fa":
        problems.append("D5 source blob pin changed")
    if rec5["source_sha256"] != "72017cd60d93bdcd9c38bc3c94e2e8f46b4c0aeba3aa90fdc229ae27fcde4f76":
        problems.append("D5 source sha256 pin changed")
    if rec5.get("historical_pre_d8", {}).get("moved_sha256") != (
        "7124a5777ed1a15c921ab70955d4c059dfc7c52d9bde789d14b7db43764d38e0"
    ):
        problems.append("D5-time moved_sha256 history was rewritten")
    pre13 = rec5.get("historical_pre_d13", {})
    if (
        pre13.get("moved_sha256")
        != "04a0555ce2898b9da61885a35c4b3065e014eeaf803119ab5526b70278532d52"
    ):
        problems.append("D8-time moved_sha256 (pre-D13) history is missing or rewritten")
    if rec5["moved_sha256"] == pre13.get("moved_sha256"):
        problems.append("D5 current moved_sha256 was not synchronized to D13")
    if not any("D13" in e["what"] for e in rec5["intentional_diff"]):
        problems.append("the D13 section is not recorded as an intentional_diff")
    rec8 = d8["documents"][SKILL]
    if rec8.get("d5_moved_sha256") != rec5["historical_pre_d8"]["moved_sha256"]:
        problems.append("D8 d5_moved_sha256 history was rewritten")
    if rec8.get("historical_pre_d13", {}).get("current_moved_sha256") != pre13.get("moved_sha256"):
        problems.append("D8 pre-D13 record is missing")
    if rec8["current_moved_sha256"] != rec5["moved_sha256"]:
        problems.append("D8 current hash is not synchronized with D5")
    return problems


# --- AC1: manual-only enablement, nothing else is opened ---------------------------------------


def test_ac1_skill_declares_the_ordered_read_only_manual_procedure() -> None:
    assert skill_problems((ROOT / SKILL).read_text(encoding="utf-8")) == []


def test_ac1_model_invocation_stays_disabled_and_nothing_is_preloaded_or_dispatched() -> None:
    assert (
        frontmatter((ROOT / SKILL).read_text(encoding="utf-8"))["disable-model-invocation"] is True
    )
    reviewer = frontmatter((ROOT / ".claude/agents/pr-reviewer.md").read_text(encoding="utf-8"))
    assert reviewer["skills"] == ["pr-review-judge"]  # unchanged declaration, still not preloaded
    assert not (ROOT / ".claude/hooks").exists()
    assert not (ROOT / ".claude/settings.json").exists()
    note = (ROOT / SKILL).read_text(encoding="utf-8").split(SECTION, 1)[0]
    for needle in ("preload", "dispatch", "実行しない", "fail-closed", "本番稼働", "I-4-4D13"):
        assert needle in note


def test_ac1_no_new_judge_script_schema_or_wrapper_was_added() -> None:
    assert scripts_problems(ROOT) == []
    assert not list((ROOT / SK).glob("*.py"))
    assert not list((ROOT / SK / "references").glob("*manual*"))


# --- AC2: the reused helper CLIs return "not OK" for bad evidence ------------------------------


@pytest.fixture(scope="module")
def d7() -> Any:
    return _load("tests/stage_i4/test_i4_4d7_open_pr_reference_evaluator.py", "d13_d7")


def _case(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def test_ac2_reference_evaluator_accepts_only_a_valid_reference(tmp_path: Path, d7: Any) -> None:
    _, ok = d7.run_reference(_case(tmp_path, "ok"), f"Closes #{d7.ISSUE}\n")
    assert (ok["decision"], ok["body_verdict"]) == ("closing_required", "valid")
    # an Issue-less / other-Issue / closed-Issue / Refs-only-without-grounds PR is never valid
    for name, body, kwargs in (
        ("other", "Closes #331\n", {}),
        ("closed", f"Closes #{d7.ISSUE}\n", {"state": "CLOSED"}),
        ("refs_only", f"Refs #{d7.ISSUE}\n", {}),
        ("none", "no reference at all\n", {}),
    ):
        _, out = d7.run_reference(_case(tmp_path, name), body, **kwargs)
        assert out["body_verdict"] != "valid", name


def test_ac2_reference_evaluator_fails_closed_on_unusable_facts(tmp_path: Path, d7: Any) -> None:
    body = f"Closes #{d7.ISSUE}\n"
    _, missing = d7.run_reference(_case(tmp_path, "missing"), body, omit=("facts",))
    assert (missing["decision"], missing["reason_code"]) == ("fail_closed", "facts_invalid")
    _, malformed = d7.run_reference(
        _case(tmp_path, "malformed"), body, facts=["not", "an", "object"]
    )
    assert (malformed["decision"], malformed["reason_code"]) == ("fail_closed", "facts_invalid")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo_with_change(tmp_path: Path, changed: str) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "keep.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    target = repo / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def run_allowed_paths(
    repo: Path, base: str, head: str, allowed: list[str], *, reviewed: str | None = None
) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPTS / "allowed_paths_review_gate.py"),
            "--pr-number",
            "1",
            "--base-ref",
            "main",
            "--current-base-sha",
            base,
            "--head-sha",
            head,
            "--reviewed-head-sha",
            reviewed or head,
            "--allowed-paths",
            json.dumps(allowed),
            "--issue-number",
            "2",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else {}


def test_ac2_allowed_paths_gate_distinguishes_ok_violation_and_stale_head(tmp_path: Path) -> None:
    repo, base, head = _repo_with_change(tmp_path, "docs/in.md")
    rc, ok = run_allowed_paths(repo, base, head, ["docs/in.md"])
    assert (rc, ok["status"]) == (0, "ok")
    rc, bad = run_allowed_paths(repo, base, head, ["tests/only.py"])
    assert rc == 1 and bad["status"] == "fail_closed" and bad["violations"]
    stale = "0" * 40
    rc, st = run_allowed_paths(repo, base, head, ["docs/in.md"], reviewed=stale)
    assert rc == 1 and st["status"] != "ok"
    rc, invalid = run_allowed_paths(repo, base, head, ["../escape"])
    assert rc == 1 and invalid["status"] != "ok"


@pytest.fixture
def wait_ci(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = _load(WAIT_CI, "d13_wait_ci_checks")
    monkeypatch.setattr(module, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(module, "fetch_required_inventory", lambda repo, pr: ({}, None, None))
    return module


def _ci_status(
    module: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str,
    live_head: str,
    checks: list[dict[str, str]],
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(module, "get_current_head_sha", lambda repo, pr: (live_head, None, None))
    monkeypatch.setattr(module, "fetch_checks", lambda repo, pr: (checks, None, None))
    code = module.main(
        [
            "--repo",
            "owner/repo",
            "--pr",
            "1",
            "--head-sha",
            head,
            "--required",
            "--interval",
            "1",
            "--timeout-seconds",
            "1",
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out.startswith("CI_WAIT_RESULT_V1_JSON=")
    return code, json.loads(out.split("=", 1)[1])


@pytest.mark.parametrize(
    ("checks", "live_head", "status"),
    [
        ([{"name": "t", "bucket": "pass"}], "abc", "passed"),
        ([{"name": "t", "bucket": "fail"}], "abc", "failed"),
        ([{"name": "t", "bucket": "cancel"}], "abc", "cancelled"),
        ([{"name": "t", "bucket": "skipping"}], "abc", "skipped_only"),
        ([{"name": "t", "bucket": "pass"}], "def", "head_sha_changed"),
        ([], "abc", "no_checks"),
        ([{"name": "t", "bucket": "pending"}], "abc", "pending_timeout"),
    ],
)
def test_ac2_head_scoped_ci_passes_only_for_a_passed_current_head(
    wait_ci: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    checks: list[dict[str, str]],
    live_head: str,
    status: str,
) -> None:
    code, payload = _ci_status(
        wait_ci, capsys, monkeypatch, head="abc", live_head=live_head, checks=checks
    )
    assert payload["status"] == status
    assert (code == 0) == (status == "passed")


def test_ac2_missing_gh_is_an_error_not_a_pass(
    wait_ci: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wait_ci, "shutil_which", lambda _: None)
    code = wait_ci.main(["--repo", "o/r", "--pr", "1", "--head-sha", "abc", "--required"])
    payload = json.loads(capsys.readouterr().out.strip().split("=", 1)[1])
    assert code != 0 and payload["status"] == "gh_error"


def test_ac2_deterministic_gate_reports_head_mismatch_and_never_exits_zero_on_missing_inputs(
    tmp_path: Path,
) -> None:
    script = SCRIPTS / "check_pr_review_gates.py"

    def run(pr_head: str, local_head: str) -> tuple[int, dict[str, str]]:
        proc = subprocess.run(
            [sys.executable, "-I", str(script), "--rule", "all", "--pr-body", "x"]
            + ["--pr-head-sha", pr_head, "--local-head-sha", local_head],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        out = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
        return proc.returncode, {g["gate_id"]: g["status"] for g in out.get("gates", [])}

    rc, same = run("abc", "abc")
    assert (
        rc == 1 and same["g4"] == "pass"
    )  # legacy g1/g2/g6 inputs are absent: checker fails closed
    assert {same["g1"], same["g2"], same["g6"]} == {"fail"}
    rc, mismatch = run("abc", "def")
    assert rc == 1 and mismatch["g4"] == "fail"
    text = (ROOT / SKILL).read_text(encoding="utf-8")
    m6 = next(ln for ln in text.splitlines() if "**M6 deterministic gate**" in ln)
    # Negative control (kept through D14): a g1/g2/g6 fail is never silently read as PASS, never
    # demoted to a warning without the explicit manual-only conditions, and M4 / M5 / M7 stay
    # mandatory. The D14 conditions themselves are pinned in test_i4_4d14_manual_review_operability.py.
    assert "legacy_gate_input_missing" not in text
    assert "`g3`／`g4`／`g5` のいずれかが `fail`／`indeterminate` なら blocker とし" in m6
    assert "`PASS` と報告せず" in m6
    assert (
        "(i)〜(iv) のいずれかを満たせない gate の fail は blocker とし `REQUEST_CHANGES` を返す"
        in m6
    )
    assert "M4／M5／M7 のいずれかが否定的・証拠不足なら" in m6
    assert "`APPROVE` は禁止" in m6
    assert "偽造せず" in m6


# --- AC3: writes are forbidden and the claim stays bounded -------------------------------------


def test_ac3_procedure_contains_no_mutating_command_and_lists_the_prohibitions() -> None:
    section = manual_section((ROOT / SKILL).read_text(encoding="utf-8"))
    prohibition, steps, rest = split_section(section)
    assert prohibition.count("gh pr comment|edit|review") == 1
    assert all(not MUTATING.search(body) for body in (*steps.values(), rest))
    for cmd in ("comment|edit|review|create|merge", "update-branch", "git push"):
        assert cmd in prohibition
    assert "Bot／Broker／controlled" in prohibition and "追加認証" in prohibition


def test_ac3_the_skill_does_not_claim_technical_write_denial() -> None:
    section = manual_section((ROOT / SKILL).read_text(encoding="utf-8"))
    assert "運用指示" in section and "主張せず" in section
    assert "OS／権限による隔離ではない" in section


# --- AC4 / AC6: the live runtime stays UNVERIFIED; nothing else is connected -------------------


def test_ac4_fixture_lane_records_the_live_manual_runtime_as_unverified(
    manifest: dict[str, Any],
) -> None:
    assert boundary_problems(manifest) == []
    assert manifest["runtime_acceptance"]["status"] == "UNVERIFIED_UNTIL_HUMAN_INVOKE"
    assert "/pr-review-judge" in manifest["runtime_acceptance"]["human_command_template"]


def test_ac4_output_contract_fields_and_step_log_are_declared() -> None:
    _, _, rest = split_section(manual_section((ROOT / SKILL).read_text(encoding="utf-8")))
    for needle in ("verdict", "reviewed_head_sha", "blockers", "warnings", "M1〜M8", "手順ログ"):
        assert needle in rest


# --- AC5: manifest, history, CI lane ------------------------------------------------------------


def test_ac5_manifest_pins_files_counts_and_the_ci_lane(manifest: dict[str, Any]) -> None:
    for rel in manifest["required_files"]:
        assert (ROOT / rel).is_file(), rel
    assert THIS in manifest["test_files"]
    assert sum(manifest["expected_ran"].values()) == manifest["totals"]["ran"]
    assert manifest["expected_collect"] == manifest["expected_ran"]
    assert manifest["deferred_tests"] == [] and manifest["totals"]["skipped"] == 0
    assert lane_problems(CI.read_text(encoding="utf-8"), manifest) == []


def test_ac5_d5_d6_d8_source_pins_and_history_are_kept() -> None:
    assert history_problems(_json(D5_MANIFEST), _json(D8_MANIFEST)) == []


def test_ac5_the_module_adds_no_skip_xfail_or_deselect() -> None:
    text = Path(__file__).read_text(encoding="utf-8")
    decorator = "@pytest" + ".mark."
    call = "pytest" + "."
    for needle in (decorator + "skip", decorator + "xfail", call + "skip(", call + "xfail("):
        assert needle not in text


# --- negative controls: each regression is detected ---------------------------------------------


def _mutated(old: str, new: str) -> list[str]:
    text = (ROOT / SKILL).read_text(encoding="utf-8")
    assert text.count(old) >= 1, old
    # every occurrence is replaced so a mention that D14 repeats inside M5 cannot hide the removal
    return skill_problems(text.replace(old, new))


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("disable-model-invocation: true", "disable-model-invocation: false", "disable-model"),
        ("disable-model-invocation: true\n", "", "disable-model"),
        ("`wait_ci_checks.py`", "`other_ci.py`", "wait_ci_checks.py"),
        ("`validate_pr_body.py --evaluate-reference-policy`", "`x`", "validate_pr_body.py"),
        ("pending_timeout", "pt", "pending_timeout"),
        ("status == passed` かつ", "status` かつ", "status passed"),
        ("`gh pr view <PR> --json headRefOid` を再取得し", "head を", "headRefOid"),
        (
            "`uv run --locked python3 .claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py",
            "`x",
            "allowed_paths_review_gate.py",
        ),
        ("技術的に拒否されるとは主張せず", "技術的に拒否される", "technical-denial"),
        ("OS／権限による隔離ではない", "OS 隔離である", "operating instruction"),
        ("blocker が 0 件で、", "", "blocker が 0 件"),
        ("M8 で head が不変", "head", "M8 で head が不変"),
        ("新しい schema field・wrapper・script は追加しない", "", "no-new-schema"),
        ("`git push|commit|merge|rebase|reset`", "`x`", "git push"),
    ],
)
def test_neg_skill_regressions_are_detected(old: str, new: str, expect: str) -> None:
    assert any(expect in p for p in _mutated(old, new)), expect


def test_neg_a_publish_or_mutation_inside_the_procedure_is_detected() -> None:
    text = (ROOT / SKILL).read_text(encoding="utf-8")
    for injected in (
        "`gh pr comment 1 --body-file x`",
        "`gh pr review 1 --approve`",
        "`gh pr merge 1`",
        "`gh pr edit 1 --body x`",
        "`gh api -X POST repos/o/r/issues/1/comments`",
        "`gh api repos/o/r/issues/1/comments -f body=x`",
        "`git push origin HEAD`",
    ):
        mutated = text.replace("8. **M8 ", f"8. **M8 {injected} ", 1)
        assert any("mutating" in p for p in skill_problems(mutated)), injected


def test_neg_swapping_or_dropping_steps_and_dispatch_are_detected() -> None:
    text = (ROOT / SKILL).read_text(encoding="utf-8")
    swapped = text.replace("3. **M3 ", "3. **MX ", 1)
    assert any("M1..M8" in p for p in skill_problems(swapped))
    dropped = text.replace("7. **M7 ", "7. **M9 ", 1)
    assert any("M1..M8" in p for p in skill_problems(dropped))
    dispatch = text.replace("8. **M8 ", "8. **M8 Agent(subagent_type=pr-reviewer) ", 1)
    assert any("SubAgent dispatch" in p for p in skill_problems(dispatch))
    assert any("section is missing" in p for p in skill_problems(text.replace(SECTION, "## X", 1)))


def test_neg_a_new_script_is_detected(tmp_path: Path) -> None:
    layout = tmp_path / SK / "scripts"
    layout.mkdir(parents=True)
    for name in EXPECTED_SCRIPTS:
        (layout / name).write_text("", encoding="utf-8")
    assert scripts_problems(tmp_path) == []
    (layout / "manual_review_judge.py").write_text("", encoding="utf-8")
    assert any("new script" in p for p in scripts_problems(tmp_path))


def test_neg_boundary_and_runtime_overclaims_are_detected(manifest: dict[str, Any]) -> None:
    for key in manifest["boundary_false"]:
        bad = copy.deepcopy(manifest)
        bad["evidence_boundary"][key] = True
        assert any(key in p for p in boundary_problems(bad)), key
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["manual_skill_runtime"] = "PASS"
    assert any("UNVERIFIED" in p for p in boundary_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["fixture_result_is_runtime_pass"] = True
    assert any("runtime PASS" in p for p in boundary_problems(bad))


def test_neg_history_rewrites_and_lane_tampering_are_detected(manifest: dict[str, Any]) -> None:
    d5, d8 = _json(D5_MANIFEST), _json(D8_MANIFEST)
    assert history_problems(d5, d8) == []
    bad = copy.deepcopy(d5)
    bad["documents"][SKILL]["source_blob_sha1"] = "0" * 40
    assert any("source blob" in p for p in history_problems(bad, d8))
    bad = copy.deepcopy(d5)
    bad["documents"][SKILL]["historical_pre_d13"]["moved_sha256"] = "0" * 64
    assert any("pre-D13" in p for p in history_problems(bad, d8))
    bad = copy.deepcopy(d5)
    bad["documents"][SKILL]["intentional_diff"].pop()
    assert any("intentional_diff" in p for p in history_problems(bad, d8))
    bad8 = copy.deepcopy(d8)
    bad8["documents"][SKILL]["current_moved_sha256"] = "0" * 64
    assert any("not synchronized" in p for p in history_problems(d5, bad8))
    ci = CI.read_text(encoding="utf-8")
    assert any("lane missing" in p for p in lane_problems(ci.replace(D13_JOB, "other"), manifest))
    wrong = copy.deepcopy(manifest)
    wrong["totals"]["ran"] += 1
    assert any("ran count" in p for p in lane_problems(ci, wrong))
