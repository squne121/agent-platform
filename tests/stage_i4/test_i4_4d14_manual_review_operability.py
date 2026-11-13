"""I-4-4D14 (#113): making the manual read-only `/pr-review-judge` usable on a healthy PR.

What this module proves: the Skill text keeps the strict required-checks path (M5 (a)), opens a
narrow path for a *proven* zero-required repository (M5 (b)) and states exactly when the legacy
g1 / g2 / g6 artifact absence is "not evaluated" instead of a blocker (M6). The real helper CLIs
(`wait_ci_checks.py`, `ci_verdict_summary.py`, `check_pr_review_gates.py`,
`allowed_paths_review_gate.py`) run for real; only `gh` is replaced by a fake executable that serves
canned read-only answers. `approve_eligible` below mirrors the documented procedure so that every
positive and negative combination is exercised against the real helper outputs.

What it does NOT prove: that a real interactive `/pr-review-judge <PR>` ran. That live runtime
acceptance (AC4) needs the user to type the command and stays UNVERIFIED here; a fixture result is
never a runtime PASS. No Reviewer SubAgent is dispatched and nothing is published.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d14-manual-review-operability-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
D5_MANIFEST = DEV / "i4-4d5-pr-review-consumers-manifest.json"
D8_MANIFEST = DEV / "i4-4d8-hookless-reviewer-manifest.json"
THIS = "tests/stage_i4/test_i4_4d14_manual_review_operability.py"
D14_JOB = "i4-4d14-manual-review-operability"
D14_MANIFEST_REL = "docs/dev/i4-4d14-manual-review-operability-manifest.json"
SK = ".claude/skills/pr-review-judge"
SKILL = f"{SK}/SKILL.md"
SCRIPTS = ROOT / SK / "scripts"
WAIT_CI = ROOT / ".claude/skills/impl-review-loop/scripts/wait_ci_checks.py"
SUMMARY = SCRIPTS / "ci_verdict_summary.py"
GATES = SCRIPTS / "check_pr_review_gates.py"
SECTION = "## Manual Read-only Review (D13)"
REPO = "owner/repo"
HEAD = "a" * 40
OTHER = "b" * 40
GH_FAKE = """#!{python}
import json, os, sys

scenario = json.load(open(os.environ["FAKE_GH_SCENARIO"]))
args = sys.argv[1:]


def fail(message):
    sys.stderr.write(message + "\\n")
    sys.exit(1)


def serve(key):
    value = scenario[key]
    if isinstance(value, dict) and "error" in value:
        fail(value["error"])
    sys.stdout.write(json.dumps(value))
    sys.exit(0)


if args[:2] == ["pr", "view"]:
    field = args[args.index("--json") + 1]
    if field == "headRefOid":
        head = scenario["live_head"]
        sys.stdout.write(head if "--jq" in args else json.dumps({{"headRefOid": head}}))
    elif field == "baseRefName":
        sys.stdout.write("main")
    sys.exit(0)
if args[:1] == ["api"] and "/rules/branches/" in args[1]:
    serve("ruleset")
if args[:1] == ["api"] and args[1].endswith("/protection/required_status_checks"):
    serve("classic")
if args[:2] == ["pr", "checks"]:
    serve("required_rows")
if args[:2] == ["api", "graphql"]:
    runs = scenario["runs"]
    if isinstance(runs, dict) and "error" in runs:
        fail(runs["error"])
    page = {{"hasNextPage": False, "endCursor": None}}
    commit = {{"oid": scenario["live_head"], "statusCheckRollup": {{"id": "R", "contexts": {{"pageInfo": page, "nodes": runs}}}}}}
    body = {{"data": {{"repository": {{"pullRequest": {{"commits": {{"nodes": [{{"commit": commit}}]}}}}}}}}}}
    sys.stdout.write(json.dumps(body))
    sys.exit(0)
fail("unexpected gh call: " + " ".join(args))
"""
NO_PROTECTION = {"error": "gh: Branch not protected (HTTP 404)"}
NO_REQUIRED_ROWS = {"error": "no required checks reported on the 'main' branch"}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def d13() -> Any:
    return _load("tests/stage_i4/test_i4_4d13_manual_pr_review.py", "d14_d13")


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


def skill_text() -> str:
    return (ROOT / SKILL).read_text(encoding="utf-8")


def step(text: str, ident: str) -> str:
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    return next(ln for ln in section.splitlines() if f"**{ident} " in ln)


# --- fake gh + real helper CLIs -----------------------------------------------------------------


def run_with_fake_gh(
    tmp_path: Path, argv: list[str], scenario: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH_FAKE.format(python=sys.executable), encoding="utf-8")
    gh.chmod(0o755)
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_SCENARIO": str(scenario_file),
    }
    return subprocess.run(
        [sys.executable, "-I", *argv],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )


def run(
    name: str,
    ok: bool = True,
    status: str = "completed",
    conclusion: str | None = "SUCCESS",
    commit: str = HEAD,
) -> dict[str, Any]:
    """A CheckRun node of the statusCheckRollup with complete provenance."""
    return {
        "__typename": "CheckRun",
        "databaseId": zlib.crc32(name.encode()) % 10**6 + 1,
        "name": name,
        "status": status.upper(),
        "conclusion": conclusion,
        "startedAt": "2026-11-13T00:00:00Z",
        "completedAt": "2026-11-13T00:05:00Z",
        "detailsUrl": f"https://example.invalid/runs/{name}",
        "checkSuite": {
            "commit": {"oid": commit},
            "workflowRun": {"event": "pull_request", "workflow": {"name": "ci"}},
        },
    }


def scenario(
    *,
    ruleset: Any = None,
    classic: Any = None,
    required_rows: Any = None,
    runs: Any = None,
    live_head: str = HEAD,
) -> dict[str, Any]:
    return {
        "live_head": live_head,
        "ruleset": [[]] if ruleset is None else ruleset,
        "classic": NO_PROTECTION if classic is None else classic,
        "required_rows": NO_REQUIRED_ROWS if required_rows is None else required_rows,
        "runs": [run("lint"), run("test")] if runs is None else runs,
    }


def m5_helpers(
    tmp_path: Path, sc: dict[str, Any], *, head: str = HEAD
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    wait_proc = run_with_fake_gh(
        tmp_path,
        [str(WAIT_CI), "--repo", REPO, "--pr", "1", "--head-sha", head]
        + ["--required", "--interval", "1", "--timeout-seconds", "1"],
        sc,
    )
    line = next(
        ln for ln in wait_proc.stdout.splitlines() if ln.startswith("CI_WAIT_RESULT_V1_JSON=")
    )
    wait = json.loads(line.split("=", 1)[1])
    summary_proc = run_with_fake_gh(
        tmp_path,
        [str(SUMMARY), "--pr", "1", "--repo", REPO, "--expected-head-sha", head],
        sc,
    )
    summary = json.loads(summary_proc.stdout) if summary_proc.stdout.strip() else {}
    return wait, summary_proc.returncode, summary


# --- the documented decision, mirrored (M5 / M6) ------------------------------------------------


def m5_ci_ok(
    wait: dict[str, Any], summary_rc: int, summary: dict[str, Any], head: str
) -> str | None:
    """Return "required" / "required_zero" when the manual CI condition holds, otherwise None."""
    if wait.get("current_head_sha") != head or summary_rc != 0:
        return None
    if wait.get("status") == "passed":
        return "required"
    if wait.get("status") != "no_checks":
        return None
    checks = summary.get("checks") or []
    excluded = set(summary.get("excluded_checks") or [])
    effective = [c for c in checks if c.get("name") not in excluded]
    if (
        summary.get("status") != "all_pass"
        or summary.get("errors") != []
        or summary.get("head_sha") != head
        or not effective
        or any(summary.get(k) for k in ("failed_checks", "pending_checks", "stale_checks"))
        or any(c.get("bucket") != "pass" for c in effective)
    ):
        return None
    return "required_zero"


ABSENT = {
    "g1": ("G1: required --ci-artifact missing or unreadable",),
    "g2": ("G2: no structured findings block found in PR body",),
    "g6": ("allowed_paths_gate block missing", "producer_role field missing"),
}


def m6_outcome(
    out: dict[str, Any],
    *,
    m4_ok: bool,
    m5_ok: bool,
    m7_ok: bool,
    ci_artifact_passed: bool = False,
) -> tuple[list[str], list[str]]:
    """(blockers, not_evaluated) for a check_pr_review_gates.py --rule all result."""
    gates = {g["gate_id"]: g for g in out["gates"]}
    blockers: list[str] = []
    not_evaluated: list[str] = []
    for gid in ("g3", "g4", "g5"):
        if gates[gid]["status"] == "fail":
            blockers.append(gid)
    if gates["g4"]["status"] != "pass":
        blockers.append("g4")
    for gid, positive in (("g1", m5_ok), ("g2", m7_ok), ("g6", m4_ok)):
        if gates[gid]["status"] != "fail":
            continue
        context = gates[gid].get("minimal_context") or ""
        absent_only = all(needle in context for needle in ABSENT[gid])
        if gid == "g6":
            absent_only = absent_only and context.count("missing") == 2
        if gid == "g1" and ci_artifact_passed:
            absent_only = False  # the file exists but is unusable: a present legacy input
        if absent_only and positive and gid not in blockers:
            not_evaluated.append(gid)
        else:
            blockers.append(gid)
    return blockers, not_evaluated


def run_gates(
    *args: str, pr_head: str = HEAD, local_head: str = HEAD, body: str = "plain PR body"
) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, "-I", str(GATES), "--rule", "all", "--pr-body", body]
        + ["--pr-head-sha", pr_head, "--local-head-sha", local_head, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def approve_eligible(
    *,
    ci: str | None,
    m4_ok: bool,
    m6: tuple[list[str], list[str]],
    m7_ok: bool,
    same_repo: bool = True,
    head_stable: bool = True,
) -> bool:
    return bool(ci) and m4_ok and not m6[0] and m7_ok and same_repo and head_stable


# --- problem functions over the Skill text ------------------------------------------------------


def skill_problems(text: str) -> list[str]:
    problems: list[str] = []
    if "disable-model-invocation: true" not in text.split("---", 2)[1]:
        problems.append("disable-model-invocation: true must stay")
    if SECTION not in text:
        return [*problems, "Manual Read-only Review section is missing"]
    m5, m6 = step(text, "M5"), step(text, "M6")
    for needle in (
        "(a)",
        "(b)",
        "status == passed",
        "status == no_checks",
        "Ruleset と Classic 双方",
        "required 0 件",
        "gh pr checks --required` 単独",
        "required 設定は変更しない",
        "all_pass",
        "errors == []",
        "excluded_checks",
        "pending_timeout",
        "gh_error",
        "no_required_evidence",
        "exit != 0",
    ):
        if needle not in m5:
            problems.append(f"M5 lost: {needle}")
    for needle in (
        "(i)〜(iv)",
        "G1: required --ci-artifact missing or unreadable",
        "G2: no structured findings block found in PR body",
        "allowed_paths_gate block missing; producer_role field missing",
        "`g4` の head 不一致",
        "`--ci-artifact` を渡していない場合",
        "`PASS` と報告せず",
        "warnings[]",
        "M4／M5／M7 のいずれかが否定的・証拠不足なら",
        "実在して矛盾・不完全",
        "偽造せず",
        "`--rule all` の strict な fail-closed 挙動は変更せず",
    ):
        if needle not in m6:
            problems.append(f"M6 lost: {needle}")
    verdict = text.split(SECTION, 1)[1].split("**verdict**", 1)[1].split("\n", 1)[0]
    for needle in ("M5 (b)", "全件 success", "blocker が 0 件"):
        if needle not in verdict:
            problems.append(f"APPROVE precondition lost: {needle}")
    if re.search(r"Agent\(|subagent_type|Task\(", text.split(SECTION, 1)[1].split("\n## ", 1)[0]):
        problems.append("the manual path references a SubAgent dispatch call")
    return problems


def job_runs(ci_text: str, job: str) -> str | None:
    jobs = yaml.safe_load(ci_text)["jobs"]
    if job not in jobs:
        return None
    return "\n".join(s.get("run", "") for s in jobs[job].get("steps", []))


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    runs = job_runs(ci_text, D14_JOB)
    if runs is None:
        return [f"CI lane missing: {D14_JOB}"]
    problems: list[str] = []
    if f"run_stage1b1_tests.py {D14_MANIFEST_REL}" not in runs:
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
    if b.get("manual_skill_runtime") != "UNVERIFIED_UNTIL_HUMAN_INVOKE":
        problems.append("the live manual Skill runtime must stay UNVERIFIED in the fixture lane")
    if b.get("fixture_result_is_runtime_pass") is not False:
        problems.append("a fixture result must not be recorded as runtime PASS")
    return problems


def blob_sha1(rel: str) -> str:
    data = (ROOT / rel).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --- AC1: M5 -------------------------------------------------------------------------------------


def test_ac1_skill_declares_both_ci_paths_and_the_precise_zero_required_proof() -> None:
    assert skill_problems(skill_text()) == []


def test_ac1_non_empty_required_checks_keep_the_required_only_decision(tmp_path: Path) -> None:
    required = {
        "ruleset": [
            [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "test", "integration_id": 15368}]
                    },
                }
            ]
        ]
    }
    rows = [{"name": "test", "bucket": "pass", "state": "SUCCESS"}]
    wait, rc, summary = m5_helpers(
        tmp_path, scenario(**required, required_rows=rows, runs=[run("test")])
    )
    assert wait["status"] == "passed" and m5_ci_ok(wait, rc, summary, HEAD) == "required"
    failing = [{"name": "test", "bucket": "fail", "state": "FAILURE"}]
    wait, rc, summary = m5_helpers(
        tmp_path,
        scenario(**required, required_rows=failing, runs=[run("test", conclusion="FAILURE")]),
    )
    assert wait["status"] == "failed" and m5_ci_ok(wait, rc, summary, HEAD) is None
    # the required context has not materialized: pending, never the zero-required path
    wait, rc, summary = m5_helpers(tmp_path, scenario(**required, runs=[run("lint")]))
    assert wait["status"] == "pending_timeout" and m5_ci_ok(wait, rc, summary, HEAD) is None


def test_ac1_proven_zero_required_with_all_success_ci_is_accepted(tmp_path: Path) -> None:
    wait, rc, summary = m5_helpers(tmp_path, scenario())
    assert (wait["status"], wait["current_head_sha"]) == ("no_checks", HEAD)
    assert (rc, summary["status"], summary["head_sha"]) == (0, "all_pass", HEAD)
    assert len(summary["checks"]) == 2 and summary["errors"] == []
    assert m5_ci_ok(wait, rc, summary, HEAD) == "required_zero"


@pytest.mark.parametrize(
    ("label", "sc"),
    [
        ("ruleset inventory 403", scenario(ruleset={"error": "HTTP 403: Forbidden"})),
        ("ruleset api error", scenario(ruleset={"error": "HTTP 500: server error"})),
        ("classic inventory 403", scenario(classic={"error": "HTTP 403: Forbidden"})),
        ("classic api error", scenario(classic={"error": "gh: connection reset"})),
        ("ruleset malformed", scenario(ruleset=[{"not": "a page"}])),
        ("empty ci", scenario(runs=[])),
        (
            "ci pending",
            scenario(runs=[run("lint"), run("test", status="in_progress", conclusion=None)]),
        ),
        ("ci failed", scenario(runs=[run("lint"), run("test", conclusion="FAILURE")])),
        ("ci cancelled", scenario(runs=[run("lint"), run("test", conclusion="CANCELLED")])),
        ("ci skipped", scenario(runs=[run("lint"), run("test", conclusion="SKIPPED")])),
        ("only skipped", scenario(runs=[run("lint", conclusion="SKIPPED")])),
        ("stale run commit", scenario(runs=[run("lint"), run("test", commit=OTHER)])),
        ("rollup api error", scenario(runs={"error": "HTTP 502: bad gateway"})),
    ],
)
def test_ac1_unproven_or_unhealthy_zero_required_ci_is_never_accepted(
    tmp_path: Path, label: str, sc: dict[str, Any]
) -> None:
    wait, rc, summary = m5_helpers(tmp_path, sc)
    assert m5_ci_ok(wait, rc, summary, HEAD) is None, label


def test_ac1_inventory_failure_is_not_read_as_an_empty_inventory(tmp_path: Path) -> None:
    wait, _, _ = m5_helpers(tmp_path, scenario(ruleset={"error": "HTTP 403: Forbidden"}))
    assert wait["status"] != "no_checks"
    wait, _, _ = m5_helpers(tmp_path, scenario(classic={"error": "HTTP 403: Forbidden"}))
    assert wait["status"] != "no_checks"


def test_ac1_stale_head_is_rejected_on_both_helpers(tmp_path: Path) -> None:
    wait, rc, summary = m5_helpers(tmp_path, scenario(live_head=OTHER))
    assert wait["status"] == "head_sha_changed" and rc == 30  # ci_verdict_summary: stale_head_sha
    assert m5_ci_ok(wait, rc, summary, HEAD) is None


def test_ac1_ambiguous_duplicate_results_do_not_pass(tmp_path: Path) -> None:
    older = run("test", conclusion="FAILURE")
    newer = run("test")
    older["completedAt"] = newer[
        "completedAt"
    ]  # indistinguishable order: the rerun stays ambiguous
    wait, rc, summary = m5_helpers(tmp_path, scenario(runs=[run("lint"), older, newer]))
    assert m5_ci_ok(wait, rc, summary, HEAD) is None


def test_ac1_required_settings_are_never_written_by_the_procedure() -> None:
    m5 = step(skill_text(), "M5")
    assert "required 設定は変更しない" in m5
    assert not re.search(r"gh api[^`]*(?:-X|--method)", m5)


# --- AC2: M6 -------------------------------------------------------------------------------------


def test_ac2_checker_stays_strict_and_reports_machine_readable_absence() -> None:
    rc, out = run_gates()
    assert rc == 1 and out["verdict"] == "REQUEST_CHANGES"  # --rule all is unchanged
    status = {g["gate_id"]: g for g in out["gates"]}
    assert status["g4"]["status"] == "pass"
    for gid in ("g1", "g2", "g6"):
        assert status[gid]["status"] == "fail"
        assert all(n in status[gid]["minimal_context"] for n in ABSENT[gid])


def test_ac2_absence_only_is_not_evaluated_but_never_reported_as_pass() -> None:
    _, out = run_gates()
    blockers, not_evaluated = m6_outcome(out, m4_ok=True, m5_ok=True, m7_ok=True)
    assert (blockers, not_evaluated) == ([], ["g1", "g2", "g6"])
    # the raw checker output still says fail for them: nothing was rewritten into pass
    assert {g["gate_id"]: g["status"] for g in out["gates"] if g["gate_id"] in ABSENT} == {
        "g1": "fail",
        "g2": "fail",
        "g6": "fail",
    }
    text = step(skill_text(), "M6")
    assert "`PASS` と報告せず" in text and "旧 g1/g2/g6 は artifact 未提供のため未評価" in text


@pytest.mark.parametrize(
    ("positive", "blocked"),
    [
        ({"m4_ok": False, "m5_ok": True, "m7_ok": True}, ["g6"]),
        ({"m4_ok": True, "m5_ok": False, "m7_ok": True}, ["g1"]),
        ({"m4_ok": True, "m5_ok": True, "m7_ok": False}, ["g2"]),
    ],
)
def test_ac2_each_legacy_gate_needs_its_own_manual_evidence(
    positive: dict[str, bool], blocked: list[str]
) -> None:
    _, out = run_gates()
    assert m6_outcome(out, **positive)[0] == blocked


def test_ac2_stale_head_gate_is_always_a_blocker() -> None:
    _, out = run_gates(pr_head=HEAD, local_head=OTHER)
    blockers, _ = m6_outcome(out, m4_ok=True, m5_ok=True, m7_ok=True)
    assert "g4" in blockers
    _, missing = run_gates(pr_head="", local_head="")
    assert "g4" in m6_outcome(missing, m4_ok=True, m5_ok=True, m7_ok=True)[0]


def test_ac2_existing_but_contradicting_legacy_artifacts_stay_blockers(tmp_path: Path) -> None:
    artifact = tmp_path / "ci.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "ci_test_selection/v1",
                "pr_head_sha": HEAD,
                "uncovered_changed_test_files": ["tests/x.py"],
            }
        ),
        encoding="utf-8",
    )
    _, out = run_gates("--ci-artifact", str(artifact))
    assert "g1" in m6_outcome(out, m4_ok=True, m5_ok=True, m7_ok=True)[0]
    _, partial = run_gates(body="allowed_paths_gate: {status: ok}")  # producer_role is missing
    assert "g6" in m6_outcome(partial, m4_ok=True, m5_ok=True, m7_ok=True)[0]
    selfreport = "```yaml\nfindings:\n  - head_sha: x\n    source_kind: k\n    evidence_refs: {self_report_only: true}\n```"
    _, weak = run_gates(body=selfreport)
    assert "g2" in m6_outcome(weak, m4_ok=True, m5_ok=True, m7_ok=True)[0]
    unreadable = tmp_path / "broken.json"
    unreadable.write_text("{", encoding="utf-8")
    _, broken = run_gates("--ci-artifact", str(unreadable))
    # same text as a missing file, so the procedure keys on whether an artifact was passed at all
    assert "G1" in next(g for g in broken["gates"] if g["gate_id"] == "g1")["minimal_context"]
    blockers, _ = m6_outcome(broken, m4_ok=True, m5_ok=True, m7_ok=True, ci_artifact_passed=True)
    assert "g1" in blockers


def test_ac2_empty_pr_body_stays_a_blocker() -> None:
    _, out = run_gates(body="")
    assert "g2" in m6_outcome(out, m4_ok=True, m5_ok=True, m7_ok=True)[0]


def test_ac2_consistent_legacy_artifacts_are_still_reported_as_pass() -> None:
    body = (
        "allowed_paths_gate: {status: ok}\nproducer_role: pr_author\n"
        f"```yaml\nfindings:\n  - head_sha: {HEAD}\n    source_kind: code\n"
        "    evidence_refs: {code_ref: a.py}\n```"
    )
    _, out = run_gates(body=body)
    status = {g["gate_id"]: g["status"] for g in out["gates"]}
    assert status["g2"] == "pass" and status["g6"] == "pass" and status["g4"] == "pass"


def test_ac2_other_consumers_of_the_strict_checker_are_untouched(manifest: dict[str, Any]) -> None:
    for rel, sha1 in manifest["unchanged_sources"].items():
        assert blob_sha1(rel) == sha1, rel
    for rel in manifest["unchanged_sources"]:
        assert not rel.endswith("SKILL.md")


# --- AC3: positive / negative matrix on real helper outputs --------------------------------------


@pytest.fixture
def m4(d13: Any, tmp_path: Path) -> Any:
    def go(allowed: list[str], *, reviewed: str | None = None) -> bool:
        repo, base, head = d13._repo_with_change(tmp_path, "docs/in.md")
        rc, out = d13.run_allowed_paths(repo, base, head, allowed, reviewed=reviewed)
        return rc == 0 and out.get("status") == "ok"

    return go


def verdict_ready(
    tmp_path: Path,
    sc: dict[str, Any],
    *,
    m4_ok: bool,
    m7_ok: bool = True,
    same_repo: bool = True,
    legacy_body: str = "plain PR body",
    head_stable: bool = True,
) -> bool:
    wait, rc, summary = m5_helpers(tmp_path, sc)
    ci = m5_ci_ok(wait, rc, summary, HEAD)
    _, gates = run_gates(body=legacy_body)
    m6 = m6_outcome(gates, m4_ok=m4_ok, m5_ok=bool(ci), m7_ok=m7_ok)
    return approve_eligible(
        ci=ci, m4_ok=m4_ok, m6=m6, m7_ok=m7_ok, same_repo=same_repo, head_stable=head_stable
    )


def test_ac3_positive_healthy_pr_can_be_approved(tmp_path: Path, m4: Any) -> None:
    assert m4(["docs/in.md"]) is True
    assert verdict_ready(tmp_path, scenario(), m4_ok=True) is True


@pytest.mark.parametrize(
    ("label", "sc", "kwargs"),
    [
        ("empty ci", scenario(runs=[]), {}),
        ("inventory error", scenario(ruleset={"error": "HTTP 403: Forbidden"}), {}),
        ("stale head", scenario(live_head=OTHER), {}),
        ("ci failed", scenario(runs=[run("t", conclusion="FAILURE")]), {}),
        ("ci pending", scenario(runs=[run("t", status="queued", conclusion=None)]), {}),
        ("ci cancelled", scenario(runs=[run("t", conclusion="CANCELLED")]), {}),
        ("ci skipped", scenario(runs=[run("t", conclusion="SKIPPED")]), {}),
        ("path violation", scenario(), {"m4_ok": False}),
        ("missing ac evidence", scenario(), {"m7_ok": False}),
        ("foreign repository", scenario(), {"same_repo": False}),
        ("head changed in m8", scenario(), {"head_stable": False}),
        ("legacy contradiction", scenario(), {"legacy_body": "allowed_paths_gate: {status: ok}"}),
    ],
)
def test_ac3_negative_cases_never_approve(
    tmp_path: Path, label: str, sc: dict[str, Any], kwargs: dict[str, Any]
) -> None:
    options = {"m4_ok": True, **kwargs}
    assert verdict_ready(tmp_path, sc, **options) is False, label


def test_ac3_allowed_paths_violation_and_stale_review_head_fail_the_real_gate(
    d13: Any, tmp_path: Path
) -> None:
    repo, base, head = d13._repo_with_change(tmp_path, "docs/in.md")
    rc, bad = d13.run_allowed_paths(repo, base, head, ["tests/only.py"])
    assert rc == 1 and bad["status"] == "fail_closed"
    rc, stale = d13.run_allowed_paths(repo, base, head, ["docs/in.md"], reviewed="0" * 40)
    assert rc == 1 and stale["status"] != "ok"


def test_ac3_foreign_repository_and_reference_stay_blockers_in_the_procedure() -> None:
    m1 = step(skill_text(), "M1")
    assert "foreign_repository" in m1 and "isCrossRepository: true" in m1
    assert "stale" in m1
    m3 = step(skill_text(), "M3")
    assert "fail_closed" in m3 and "body_verdict: block|repair" in m3


def test_ac3_a_subjective_open_question_may_end_in_human_review_not_approve() -> None:
    verdict = skill_text().split(SECTION, 1)[1].split("**verdict**", 1)[1].split("\n", 1)[0]
    assert "HUMAN_REVIEW_REQUIRED" in verdict and "semantic な判断" in verdict


# --- AC4 / AC6: the live runtime stays UNVERIFIED; nothing else is connected --------------------


def test_ac4_fixture_lane_records_the_live_manual_runtime_as_unverified(
    manifest: dict[str, Any],
) -> None:
    assert boundary_problems(manifest) == []
    assert manifest["runtime_acceptance"]["status"] == "UNVERIFIED_UNTIL_HUMAN_INVOKE"
    assert "/pr-review-judge" in manifest["runtime_acceptance"]["human_command_template"]


def test_ac6_manual_only_no_dispatch_no_publish_are_kept(d13: Any) -> None:
    fm = d13.frontmatter(skill_text())
    assert fm["disable-model-invocation"] is True
    section = skill_text().split(SECTION, 1)[1].split("\n## ", 1)[0]
    _, steps, rest = d13.split_section(section)
    assert list(steps) == d13.STEP_IDS
    assert all(not d13.MUTATING.search(body) for body in (*steps.values(), rest))
    assert d13.scripts_problems(ROOT) == []
    assert not (ROOT / ".claude/hooks").exists()
    reviewer = d13.frontmatter((ROOT / ".claude/agents/pr-reviewer.md").read_text(encoding="utf-8"))
    assert reviewer["skills"] == ["pr-review-judge"]
    assert d13.skill_problems(skill_text()) == []


# --- AC5: manifest, history, CI lane ------------------------------------------------------------


def test_ac5_manifest_pins_files_counts_and_the_ci_lane(manifest: dict[str, Any]) -> None:
    for rel in manifest["required_files"]:
        assert (ROOT / rel).is_file(), rel
    assert THIS in manifest["test_files"]
    assert sum(manifest["expected_ran"].values()) == manifest["totals"]["ran"]
    assert manifest["expected_collect"] == manifest["expected_ran"]
    assert manifest["deferred_tests"] == [] and manifest["totals"]["skipped"] == 0
    assert lane_problems(CI.read_text(encoding="utf-8"), manifest) == []


def test_ac5_d5_d6_d8_d13_source_pins_and_history_are_kept(d13: Any) -> None:
    d5, d8 = _json(D5_MANIFEST), _json(D8_MANIFEST)
    assert d13.history_problems(d5, d8) == []  # D5 / D8 / D13 history is intact
    rec5, rec8 = d5["documents"][SKILL], d8["documents"][SKILL]
    current = hashlib.sha256((ROOT / SKILL).read_bytes()).hexdigest()
    assert rec5["moved_sha256"] == current == rec8["current_moved_sha256"]
    assert rec5["moved_lines"] == (ROOT / SKILL).read_bytes().count(b"\n")
    assert rec5["historical_pre_d14"]["moved_sha256"] != current
    assert (
        rec8["historical_pre_d14"]["current_moved_sha256"]
        == rec5["historical_pre_d14"]["moved_sha256"]
    )
    assert rec5["intentional_diff"][2]["moved"] in skill_text()
    assert rec5["source_blob_sha1"] == "db2ae9d1f17498fa358528f552974f7e2666c8fa"


def test_ac5_the_module_adds_no_skip_xfail_or_deselect() -> None:
    text = Path(__file__).read_text(encoding="utf-8")
    decorator = "@pytest" + ".mark."
    call = "pytest" + "."
    for needle in (decorator + "skip", decorator + "xfail", call + "skip(", call + "xfail("):
        assert needle not in text


# --- negative controls: each regression is detected ---------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("disable-model-invocation: true", "disable-model-invocation: false", "disable-model"),
        ("Ruleset と Classic 双方", "Ruleset", "Ruleset と Classic"),
        ("`gh pr checks --required` 単独", "`x`", "gh pr checks --required"),
        ("required 設定は変更しない", "", "required 設定"),
        ("`errors == []`", "`x`", "errors == []"),
        ("`status == all_pass`", "`x`", "all_pass"),
        ("`PASS` と報告せず", "", "PASS"),
        ("(i)〜(iv)", "(i)", "(i)〜(iv)"),
        ("`g4` の head 不一致", "`g4`", "g4"),
        ("M4／M5／M7 のいずれかが否定的・証拠不足なら", "", "M4／M5／M7"),
        ("実在して矛盾・不完全", "", "矛盾"),
        ("偽造せず", "", "偽造"),
        ("M5 (b)", "", "M5 (b)"),
    ],
)
def test_neg_skill_regressions_are_detected(old: str, new: str, expect: str) -> None:
    text = skill_text()
    assert old in text, old
    assert any(expect in p for p in skill_problems(text.replace(old, new))), expect


def test_neg_a_dispatch_inside_the_procedure_is_detected() -> None:
    mutated = skill_text().replace("8. **M8 ", "8. **M8 Agent(subagent_type=pr-reviewer) ", 1)
    assert any("SubAgent dispatch" in p for p in skill_problems(mutated))


def test_neg_the_decision_functions_reject_hand_edited_helper_output(tmp_path: Path) -> None:
    wait, rc, summary = m5_helpers(tmp_path, scenario())
    assert m5_ci_ok(wait, rc, summary, HEAD) == "required_zero"
    for mutate in (
        lambda w, s: w.update(status="pending_timeout"),
        lambda w, s: w.update(current_head_sha=OTHER),
        lambda w, s: s.update(status="no_required_evidence"),
        lambda w, s: s.update(errors=[{"kind": "gh_other_error"}]),
        lambda w, s: s.update(checks=[]),
        lambda w, s: s.update(failed_checks=["lint"]),
        lambda w, s: s.update(head_sha=OTHER),
    ):
        w, s = copy.deepcopy(wait), copy.deepcopy(summary)
        mutate(w, s)
        assert m5_ci_ok(w, rc, s, HEAD) is None
    assert m5_ci_ok(wait, 10, summary, HEAD) is None


def test_neg_boundary_overclaims_and_history_or_lane_tampering_are_detected(
    manifest: dict[str, Any], d13: Any
) -> None:
    for key in manifest["boundary_false"]:
        bad = copy.deepcopy(manifest)
        bad["evidence_boundary"][key] = True
        assert any(key in p for p in boundary_problems(bad)), key
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["manual_skill_runtime"] = "PASS"
    assert any("UNVERIFIED" in p for p in boundary_problems(bad))
    d5, d8 = _json(D5_MANIFEST), _json(D8_MANIFEST)
    bad5 = copy.deepcopy(d5)
    bad5["documents"][SKILL]["historical_pre_d13"]["moved_sha256"] = "0" * 64
    assert any("pre-D13" in p for p in d13.history_problems(bad5, d8))
    ci = CI.read_text(encoding="utf-8")
    assert any("lane missing" in p for p in lane_problems(ci.replace(D14_JOB, "other"), manifest))
    wrong = copy.deepcopy(manifest)
    wrong["totals"]["ran"] += 1
    assert any("ran count" in p for p in lane_problems(ci, wrong))
