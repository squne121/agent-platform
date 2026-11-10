"""I-4-4D6 (#99) closure: pr-review-judge CI evidence / deterministic gate execution core.

Migrated byte-for-byte from the pinned source (three recorded, necessary edits) with its two source
test modules and six CI fixtures. The closure checks source parity, the measured collected / ran
counts, the real CLI behaviour (fake `gh` on PATH inside tmp_path; read-only), the schema
fail-closed fix, the D5 history sync and the owner inventory of every unmigrated dependency.

It is document parity plus real pytest / real CLI runs. It is not a Skill preload, a Reviewer
dispatch, a verdict publish, a Final Gate or a live GitHub result.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d6-pr-review-core-manifest.json"
D5_MANIFEST = DEV / "i4-4d5-pr-review-consumers-manifest.json"
D4_MANIFEST = DEV / "i4-4d4-skill-agents-readonly-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = "tests/stage_i4/test_i4_4d6_pr_review_core.py"
D6_JOB = "i4-4d6-pr-review-core"
D6_MANIFEST_REL = "docs/dev/i4-4d6-pr-review-core-manifest.json"
SK = ".claude/skills/pr-review-judge"
SUMMARY = f"{SK}/scripts/ci_verdict_summary.py"
GATES = f"{SK}/scripts/check_pr_review_gates.py"
SCHEMA = f"{SK}/references/pr-review-gate-result-schema.yml"
T_SUMMARY = f"{SK}/scripts/tests/test_ci_verdict_summary.py"
T_GATES = f"{SK}/scripts/tests/test_check_pr_review_gates_indeterminate.py"
FIXTURE_DIR = f"{SK}/scripts/tests/fixtures/ci_verdict"
FIXTURES = tuple(
    f"{FIXTURE_DIR}/{n}.json"
    for n in (
        "checks_all_pass",
        "checks_ambiguous",
        "checks_failed",
        "checks_pending",
        "pr_view_pass",
        "pr_view_stale",
    )
)
AUTHORITY = ".claude/skills/impl-review-loop/scripts/wait_ci_checks.py"
SOURCE_COMMIT = "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
# Pins fixed by the Issue contract (#99): the Git blobs of the source tag.
ISSUE_SOURCE_BLOBS = {
    SUMMARY: "6d03dbccbfbac44756435542ff0f6db5ea2a2d4d",
    GATES: "d6a1021f1d72b56862388608704a70fb36f7d8db",
    T_SUMMARY: "7b31b08bb3ab975ea41398f211e8e761ce5bd177",
    T_GATES: "7ea668da901c81a9bad0e89f58bcfd9686b535c7",
    SCHEMA: "97dbc6ba9105ac4c78ee3b4cec1826edd3b1a709",
    f"{FIXTURE_DIR}/checks_all_pass.json": "6fe7c286a816d4bb5ac7317bb3447a91cd0b63fe",
    f"{FIXTURE_DIR}/checks_ambiguous.json": "58fd76f82ea1312e394f83bf49b08173bb669388",
    f"{FIXTURE_DIR}/checks_failed.json": "b4588b02d966e9d3ed64cc141acec4141bb7c2bf",
    f"{FIXTURE_DIR}/checks_pending.json": "c4d44f535f5771af3349e8064314136f40bd01ba",
    f"{FIXTURE_DIR}/pr_view_pass.json": "befe84fb1160599bbd8df3fffa76f6a0144d979d",
    f"{FIXTURE_DIR}/pr_view_stale.json": "840acf43ea212dd7c37effb749b36b0fa9a926c6",
}
EXPECTED_COLLECT = {T_SUMMARY: 167, T_GATES: 6}
# Not migrated in D6: they stay absent (no empty stub) and keep an owner in the manifest.
STILL_ABSENT = (
    f"{SK}/scripts/ci_verdict_summary_v2.py",
    f"{SK}/scripts/generate_ci_test_selection_artifact.py",
    f"{SK}/scripts/wait_pr_checks.py",
    f"{SK}/scripts/tests/test_check_pr_review_gates.py",
    "scripts/ci/ci_job_snapshot.py",
    "scripts/ci/python_test_plan.py",
    ".claude/skills/open-pr",
    ".claude/hooks/pr_reviewer_guard.py",
)
HEAD = "a" * 40


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


d2 = _load("tests/stage_i4/test_i4_4d2_step2_verification.py", "d2_helpers_for_i4_4d6")
d4 = _load("tests/stage_i4/test_i4_4d4_skill_agents_readonly.py", "d4_helpers_for_i4_4d6")
document_problems = d2.document_problems
job_runs = d2.job_runs
frontmatter = d4.frontmatter


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def _collect(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


# --- fake gh (read-only; records every call) ------------------------------------------------

FAKE_GH = """#!{python}
import json, os, sys
cfg = json.load(open(os.environ["FAKE_GH_CONFIG"]))
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
key = (
    "pr_view" if a[:2] == ["pr", "view"]
    else "graphql" if a[:2] == ["api", "graphql"]
    else "run_view" if a[:2] == ["run", "view"]
    else None
)
if key is None or key not in cfg:
    sys.stderr.write("unexpected gh call")
    sys.exit(99)
r = cfg[key]
sys.stdout.write(r.get("stdout", ""))
sys.stderr.write(r.get("stderr", ""))
sys.exit(r.get("rc", 0))
"""


def _node(
    run_id: int, name: str, status: str, conclusion: str | None, head: str = HEAD
) -> dict[str, Any]:
    done = status == "COMPLETED"
    return {
        "__typename": "CheckRun",
        "databaseId": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": "2026-11-10T00:00:00Z",
        "completedAt": "2026-11-10T00:01:00Z" if done else None,
        "detailsUrl": f"https://github.com/owner/repo/actions/runs/{run_id}",
        "checkSuite": {
            "commit": {"oid": head},
            "workflowRun": {"event": "pull_request", "workflow": {"name": "CI"}},
        },
    }


def _gql(nodes: list[dict[str, Any]], head: str = HEAD) -> str:
    rollup = {
        "id": "rollup-1",
        "contexts": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes},
    }
    commit = {"oid": head, "statusCheckRollup": rollup}
    return json.dumps(
        {"data": {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": commit}]}}}}}
    )


def _pr_view(head: str = HEAD) -> dict[str, Any]:
    return {"rc": 0, "stdout": json.dumps({"headRefOid": head})}


def run_summary(
    tmp_path: Path, cfg: dict[str, Any], *, expected: str = HEAD, extra: tuple[str, ...] = ()
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], list[list[str]]]:
    """Run the real ci_verdict_summary.py with a fake gh; return result, JSON and the gh calls."""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    bin_dir = work / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
    gh.chmod(0o755)
    (work / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
    log = work / "gh.log"
    log.write_text("", encoding="utf-8")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        FAKE_GH_CONFIG=str(work / "cfg.json"),
        FAKE_GH_LOG=str(log),
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / SUMMARY),
            "--pr",
            "7",
            "--repo",
            "owner/repo",
            "--expected-head-sha",
            expected,
            *extra,
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return proc, out, calls


def read_only_problems(calls: list[list[str]]) -> list[str]:
    """Every gh call is a read: pr view / run view / a GraphQL query (never a mutation)."""
    problems: list[str] = []
    for argv in calls:
        head = argv[:2]
        if head == ["api", "graphql"]:
            query = " ".join(a for a in argv if a.startswith("query="))
            if re.search(r"\bmutation\b", query, re.IGNORECASE) or "--method" in argv:
                problems.append(f"GraphQL write form: {argv[:4]}")
        elif head not in (["pr", "view"], ["run", "view"]):
            problems.append(f"non-read gh call: {argv[:3]}")
    return problems


# --- gate CLI helpers -----------------------------------------------------------------------

GOOD_ARTIFACT = {
    "schema_version": "ci_test_selection/v1",
    "pr_head_sha": "abc",
    "uncovered_changed_test_files": [],
}
GOOD_BODY = (
    "```yaml\n"
    "findings:\n"
    "  - head_sha: abc\n"
    "    source_kind: code_review\n"
    "    evidence_refs:\n"
    "      pr_head_sha: abc\n"
    "```\n"
    "allowed_paths_gate: pass\n"
    "producer_role: review_subagent\n"
)


def run_gates(
    script: Path,
    tmp_path: Path,
    *,
    artifact: dict[str, Any] | None = GOOD_ARTIFACT,
    body: str = GOOD_BODY,
    pr_head: str = "abc",
    local_head: str = "abc",
) -> tuple[int, dict[str, Any], str]:
    args = [sys.executable, "-I", str(script), "--rule", "all"]
    if artifact is not None:
        art = tmp_path / "artifact.json"
        art.write_text(json.dumps(artifact), encoding="utf-8")
        args += ["--ci-artifact", str(art)]
    args += ["--pr-body", body, "--pr-head-sha", pr_head, "--local-head-sha", local_head]
    proc = subprocess.run(args, capture_output=True, text=True, check=False, timeout=120)
    out = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
    return proc.returncode, out, proc.stderr


def gate_status(out: dict[str, Any]) -> dict[str, str]:
    return {g["gate_id"]: g["status"] for g in out.get("gates", [])}


def schema_fail_closed_problems(script_text: str, tmp_path: Path) -> list[str]:
    """The consumer must exit 2 (never APPROVE / exit 0) when its schema cannot be trusted."""
    layout = tmp_path / "layout"
    (layout / "scripts").mkdir(parents=True, exist_ok=True)
    (layout / "references").mkdir(exist_ok=True)
    script = layout / "scripts" / "check_pr_review_gates.py"
    script.write_text(script_text, encoding="utf-8")
    schema = layout / "references" / "pr-review-gate-result-schema.yml"
    cases = {
        "missing": None,
        "malformed yaml": "schema: [unclosed\n  : :",
        "no schema key": "foo: bar\n",
        "empty required": "schema:\n  required: []\n",
    }
    problems: list[str] = []
    for label, content in cases.items():
        if schema.exists():
            schema.unlink()
        if content is not None:
            schema.write_text(content, encoding="utf-8")
        code, _, err = run_gates(script, tmp_path)
        if code != 2 or "fail-closed" not in err:
            problems.append(f"schema {label}: exit {code}, not fail-closed")
    shutil.copy2(ROOT / SCHEMA, schema)
    code, out, err = run_gates(script, tmp_path)
    if code != 0 or out.get("verdict") != "APPROVE":
        problems.append(f"real schema no longer validates the APPROVE control: exit {code} {err}")
    return problems


# --- problem functions (pure: they return a list of messages, [] means OK) -------------------


def issue_pin_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if m["source"]["commit"] != SOURCE_COMMIT:
        problems.append("source commit pin is not the peeled commit of the source tag")
    recorded = {**m["documents"], **m["verbatim_files"]}
    for rel, blob in ISSUE_SOURCE_BLOBS.items():
        if recorded.get(rel, {}).get("source_blob_sha1") != blob:
            problems.append(f"source blob is not the Issue-fixed pin: {rel}")
    return problems


def parity_problems(m: dict[str, Any], root: Path) -> list[str]:
    return d4.parity_problems(m, root)


EDIT_REQUIRED = {
    GATES: ("fail-closed", "Schema unavailable"),
    T_SUMMARY: ("owner/repo",),
    SCHEMA: ("owner/repo",),
}


def intentional_diff_problems(m: dict[str, Any]) -> list[str]:
    """Only the three recorded, justified edits exist; everything else is byte-identical."""
    problems: list[str] = []
    if set(m["documents"]) != set(EDIT_REQUIRED):
        problems.append(f"edited-file set changed: {sorted(m['documents'])}")
    for rel, needles in EDIT_REQUIRED.items():
        rec = m["documents"].get(rel, {})
        diffs = rec.get("intentional_diff", [])
        joined = "\n".join(e.get("moved", "") for e in diffs)
        if not all(n in joined for n in needles):
            problems.append(f"intentional_diff lost its content: {rel}")
        for e in diffs:
            if not e.get("what") or not e.get("reason"):
                problems.append(f"intentional_diff without what / reason: {rel}")
    legacy = m["source"]["repository"]  # the legacy slug, read from the manifest (no literal)
    records = [r for r in m["documents"].values() if "intentional_diff" in r]
    sources = "\n".join(e.get("source", "") for r in records for e in r["intentional_diff"])
    if legacy not in sources:
        problems.append("the original legacy-URL text was not recorded")
    if len(m["documents"].get(T_SUMMARY, {}).get("intentional_diff", [])) != 3:
        problems.append("test_ci_verdict_summary.py must record exactly the 3 URL / repo edits")
    return problems


def authority_problems(m: dict[str, Any], root: Path) -> list[str]:
    """wait_ci_checks.py stays the only authoritative required-CI evaluator."""
    problems: list[str] = []
    if m["authority"]["required_ci_evaluator"] != AUTHORITY:
        problems.append("required-CI authority is no longer wait_ci_checks.py")
    if not (root / AUTHORITY).is_file():
        problems.append("authoritative evaluator is missing")
    for rel in (SUMMARY, GATES):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        imported = {
            *(a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names),
            *(n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)),
        }
        if any("wait_ci_checks" in i for i in imported):
            problems.append(f"{rel} must not re-implement or wrap the authoritative evaluator")
    if "auxiliary" not in m["authority"]["ci_verdict_summary_role"]:
        problems.append("ci_verdict_summary.py role is not recorded as auxiliary")
    if "legacy" not in json.dumps(m["legacy_ci_contract"]):
        problems.append("legacy CI-name contract is not classified")
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
    for needed in (
        "ci_verdict_summary_v2.py",
        "ci_job_snapshot.py",
        "generate_ci_test_selection_artifact.py",
        "python_test_plan.py",
        "wait_pr_checks.py",
        "test_check_pr_review_gates.py",
        "validate_pr_body.py",
        "pr_reviewer_guard.py",
    ):
        if needed not in text:
            problems.append(f"unmigrated dependency not recorded: {needed}")
    owners = "\n".join(e["owner"] for e in deps)
    for slice_id in ("D7", "D8", "D9"):
        if slice_id not in owners:
            problems.append(f"no follow-up owner names {slice_id}")
    for absent in STILL_ABSENT:
        if (root / absent).exists():
            problems.append(f"recorded as unmigrated but present (update the record): {absent}")
    return problems


def boundary_problems(m: dict[str, Any], root: Path) -> list[str]:
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
        "github_mutation_by_ci_helpers",
        "git_ref_mutation",
        "hook_effective_protection",
        "open_pr_evaluator_wired",
        "ci_v2_or_generator_wired",
        "reviewer_hook_wired",
        "disable_model_invocation_released",
    ):
        if b.get(key) is not False:
            problems.append(f"evidence boundary overclaims: {key}")
    for rel, name in (
        (f"{SK}/SKILL.md", "pr-review-judge"),
        (".claude/skills/impl-review-loop/SKILL.md", "impl-review-loop"),
    ):
        fm = frontmatter(root / rel)
        if fm.get("name") != name or fm.get("disable-model-invocation") is not True:
            problems.append(f"{name} must keep disable-model-invocation: true")
    for rel in (SUMMARY, GATES):
        text = (root / rel).read_text(encoding="utf-8")
        if re.search(
            r"\[\s*\"gh\"\s*\]\s*\+\s*\[?\s*\"(pr|issue)\"\s*,\s*\"(comment|review|edit|merge)",
            text,
        ):
            problems.append(f"{rel} contains a GitHub write form")
    return problems


def stale_absence_problems(root: Path) -> list[str]:
    """No earlier slice may still assert that the D6 files do not exist."""
    problems: list[str] = []
    d6_names = r"(ci_verdict_summary\.py|check_pr_review_gates\.py|pr-review-gate-result-schema)"
    for path in sorted((root / "tests" / "stage_i4").glob("test_i4_4d[1-5]_*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"(STILL_ABSENT|for absent in) *=? *\((.*?)\)", text, re.DOTALL):
            if re.search(d6_names, m.group(2)):
                problems.append(f"stale absence assertion in {path.name}")
        if re.search(rf"assert not [^\n]*{d6_names}", text):
            problems.append(f"stale absence assertion in {path.name}")
    return problems


def source_test_problems(m: dict[str, Any], root: Path) -> list[str]:
    """No skip / xfail marker; collected counts equal the manifest."""
    problems: list[str] = []
    for rel in m["test_files"]:
        text = (root / rel).read_text(encoding="utf-8") if (root / rel).is_file() else ""
        if d4.SKIP_RE.search(text):
            problems.append(f"skip / xfail marker in a source test file: {rel}")
    if m["deferred_tests"]:
        problems.append("a deferred source test is recorded")
    if m["expected_collect"] != EXPECTED_COLLECT or m["expected_ran"] != EXPECTED_COLLECT:
        problems.append("expected_collect / expected_ran differ from the measured 167 + 6")
    t = m["totals"]
    if (t["source_collected"], t["deferred"], t["ran"], t["skipped"], t["xfailed"]) != (
        173,
        0,
        173,
        0,
        0,
    ):
        problems.append("totals are not 173 collected / 0 deferred / 173 ran")
    return problems


def d5_sync_problems(m: dict[str, Any], d5: dict[str, Any]) -> list[str]:
    """D5 values stay (108 / 108 / 0) and its pre-D6 'unmigrated' record is kept as history."""
    problems: list[str] = []
    t = d5["totals"]
    if (t["source_collected"], t["deferred"], t["ran"], t["skipped"], t["xfailed"]) != (
        108,
        0,
        108,
        0,
        0,
    ):
        problems.append("D5 totals are not 108 collected / 0 deferred / 108 ran")
    hist = d5.get("historical_pre_d6", {})
    if not hist.get("hard_dependencies") or "ci_verdict_summary.py" not in json.dumps(hist):
        problems.append("D5 historical_pre_d6 (the unmigrated record) was lost")
    migrated = d5.get("migrated_by_d6", {})
    if set(migrated.get("files", [])) != {SUMMARY, GATES, SCHEMA}:
        problems.append("D5 migrated_by_d6 does not name the migrated script / schema files")
    if migrated.get("manifest") != D6_MANIFEST_REL:
        problems.append("D5 migrated_by_d6 does not point at the D6 manifest")
    live = "\n".join(e["reference"] for e in d5["unmigrated_references"]["hard_dependencies"])
    live = re.sub(r"\([^()]*were migrated by I-4-4D6\)", "", live)
    if "ci_verdict_summary.py" in live.replace("ci_verdict_summary_v2.py", "") or (
        "check_pr_review_gates.py" in live.replace("test_check_pr_review_gates.py", "")
    ):
        problems.append("D5 live record still lists a migrated script as unmigrated")
    if m["d5_sync"]["d5_totals_kept"] != {"collected": 108, "ran": 108, "deferred": 0}:
        problems.append("D6 manifest lost the D5 108 / 108 / 0 record")
    return problems


def history_problems() -> list[str]:
    problems: list[str] = []
    d4m = _json(D4_MANIFEST)
    hist = d4m.get("historical_pre_d5", {}).get("totals", {})
    if (hist.get("source_collected"), hist.get("deferred"), hist.get("ran")) != (108, 1, 107):
        problems.append("D4 historical 108 / 1 / 107 was lost")
    c2 = _json(DEV / "i4-4c2-intake-capsule-manifest.json")
    if (c2["totals"]["ran"], c2["totals"]["deferred"]) != (118, 1):
        problems.append("C2 residual deferred (118 ran / 1 deferred) changed")
    if "live_github" not in json.dumps(c2["deferred_tests"]).lower() and "real_github" not in (
        json.dumps(c2["deferred_tests"]).lower()
    ):
        problems.append("C2 live canary deferral lost its identity")
    b1 = _json(DEV / "i4-4b1-vc-adjudication-manifest.json")
    if b1["historical_pre_d3"]["deferred_source_tests"] != 5:
        problems.append("B1 historical_pre_d3 (5 deferred) was lost")
    b2 = _json(DEV / "i4-4b2-vc-step4-tests-manifest.json")
    if (b2["historical_pre_d2"]["totals"]["ran"], b2["totals"]["ran"]) != (298, 368):
        problems.append("B2 298 historical / 368 current changed")
    return problems


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    runs = job_runs(ci_text, D6_JOB)
    if runs is None:
        return [f"CI lane missing: {D6_JOB}"]
    problems: list[str] = []
    if f"run_stage1b1_tests.py {D6_MANIFEST_REL}" not in runs:
        problems.append("CI lane does not run its manifest")
    t = m["totals"]
    if f"{t['deferred']} deferred with reasons, {t['ran']} ran, 0 skipped" not in runs:
        problems.append("CI lane does not pin the manifest ran count")
    if "tests/stage_i4" not in runs:
        problems.append("CI lane does not run the closure tests")
    if "(skipped|xfailed|xpassed|deselected)" not in runs:
        problems.append("CI lane does not reject skip / xfail / deselect")
    return problems


def _copy_tree(m: dict[str, Any], tmp_path: Path) -> None:
    for rel in [*m["documents"], *m["verbatim_files"], AUTHORITY, f"{SK}/SKILL.md"]:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)


# --- AC1: source parity, pins, owners --------------------------------------------------------


def test_ac1_files_reproduce_the_pinned_source(manifest: dict[str, Any]) -> None:
    assert parity_problems(manifest, ROOT) == []
    assert issue_pin_problems(manifest) == []
    assert len(manifest["documents"]) == 3 and len(manifest["verbatim_files"]) == 8
    assert set(manifest["required_fixtures"]) == set(FIXTURES)


def test_ac1_every_difference_from_the_source_is_recorded_and_justified(
    manifest: dict[str, Any],
) -> None:
    assert intentional_diff_problems(manifest) == []


def test_ac1_unmigrated_dependencies_have_owner_rerun_condition_and_stay_absent(
    manifest: dict[str, Any],
) -> None:
    assert dependency_problems(manifest, ROOT) == []


def test_ac1_d5_record_is_synced_and_its_history_is_kept(manifest: dict[str, Any]) -> None:
    assert d5_sync_problems(manifest, _json(D5_MANIFEST)) == []
    assert stale_absence_problems(ROOT) == []


# --- AC2: real source tests and real CLI negatives -------------------------------------------


def test_ac2_source_tests_run_173_with_nothing_deferred() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/run_stage1b1_tests.py", D6_MANIFEST_REL],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "173 collected, 0 deferred with reasons, 173 ran, 0 skipped" in out, out
    assert "173 passed" in out and "deselected" not in out, out
    assert not any(w in out for w in ("xfailed", "xpassed", "skipped,")), out


def test_ac2_collected_counts_are_measured_per_file(manifest: dict[str, Any]) -> None:
    r = _collect(T_SUMMARY, T_GATES)
    ids = [ln for ln in r.stdout.splitlines() if "::" in ln]
    counts = {f: sum(1 for i in ids if i.startswith(f + "::")) for f in (T_SUMMARY, T_GATES)}
    assert r.returncode == 0, r.stdout + r.stderr
    assert counts == manifest["expected_collect"] == EXPECTED_COLLECT
    assert source_test_problems(manifest, ROOT) == []


def test_ac2_current_head_all_success_passes(tmp_path: Path) -> None:
    cfg = {
        "pr_view": _pr_view(),
        "graphql": {
            "stdout": _gql(
                [
                    _node(1, "build", "COMPLETED", "SUCCESS"),
                    _node(2, "test", "COMPLETED", "SUCCESS"),
                ]
            )
        },
    }
    proc, out, calls = run_summary(tmp_path, cfg)
    assert (proc.returncode, out["status"]) == (0, "all_pass"), proc.stderr
    assert out["schema"] == "CI_VERDICT_SUMMARY_V1" and out["head_sha"] == HEAD
    assert read_only_problems(calls) == []
    assert len(calls) == 2  # pr view + one GraphQL query


@pytest.mark.parametrize(
    ("label", "cfg", "expected_exit", "expected_status"),
    [
        (
            "stale PR head",
            {
                "pr_view": _pr_view("b" * 40),
                "graphql": {"stdout": _gql([_node(1, "build", "COMPLETED", "SUCCESS")])},
            },
            30,
            "stale_head_sha",
        ),
        (
            "check run bound to an older commit",
            {
                "pr_view": _pr_view(),
                "graphql": {
                    "stdout": _gql([_node(1, "build", "COMPLETED", "SUCCESS", head="c" * 40)])
                },
            },
            30,
            "stale_head_sha",
        ),
        (
            "no checks at all (missing required evidence)",
            {"pr_view": _pr_view(), "graphql": {"stdout": _gql([])}},
            10,
            "no_required_evidence",
        ),
        (
            "skipped is not approve evidence",
            {
                "pr_view": _pr_view(),
                "graphql": {"stdout": _gql([_node(1, "build", "COMPLETED", "SKIPPED")])},
            },
            10,
            "failed",
        ),
        (
            "neutral is not approve evidence",
            {
                "pr_view": _pr_view(),
                "graphql": {"stdout": _gql([_node(1, "build", "COMPLETED", "NEUTRAL")])},
            },
            10,
            "failed",
        ),
        (
            "one failing check",
            {
                "pr_view": _pr_view(),
                "graphql": {
                    "stdout": _gql(
                        [
                            _node(1, "build", "COMPLETED", "SUCCESS"),
                            _node(2, "test", "COMPLETED", "FAILURE"),
                        ]
                    )
                },
            },
            10,
            "failed",
        ),
        (
            "pending check",
            {
                "pr_view": _pr_view(),
                "graphql": {"stdout": _gql([_node(1, "build", "IN_PROGRESS", None)])},
            },
            20,
            "pending_or_queued",
        ),
        (
            "gh authentication error on pr view",
            {"pr_view": {"rc": 1, "stderr": "HTTP 401: authentication required"}},
            40,
            "gh_error",
        ),
        (
            "gh error on GraphQL",
            {"pr_view": _pr_view(), "graphql": {"rc": 1, "stderr": "HTTP 502 Bad Gateway"}},
            40,
            "gh_error",
        ),
        (
            "malformed GraphQL shape",
            {"pr_view": _pr_view(), "graphql": {"stdout": json.dumps({"data": {}})}},
            40,
            "gh_error",
        ),
        (
            "GraphQL output is not JSON",
            {"pr_view": _pr_view(), "graphql": {"stdout": "not json"}},
            40,
            "gh_error",
        ),
    ],
)
def test_ac2_ci_evidence_failure_modes_fail_closed(
    tmp_path: Path, label: str, cfg: dict[str, Any], expected_exit: int, expected_status: str
) -> None:
    proc, out, calls = run_summary(tmp_path, cfg)
    assert (proc.returncode, out.get("status")) == (expected_exit, expected_status), label
    assert proc.returncode != 0, label
    assert read_only_problems(calls) == [], label


def test_ac2_auth_and_missing_check_name_are_classified(tmp_path: Path) -> None:
    proc, out, _ = run_summary(
        tmp_path, {"pr_view": {"rc": 1, "stderr": "HTTP 401: authentication required"}}
    )
    assert proc.returncode == 40 and [e["kind"] for e in out["errors"]] == ["auth_failed"]
    cfg = {
        "pr_view": _pr_view(),
        "graphql": {"stdout": _gql([_node(1, "build", "COMPLETED", "SUCCESS")])},
    }
    proc, out, _ = run_summary(tmp_path / "second", cfg, extra=("--check-name", "absent"))
    assert proc.returncode == 40
    assert [e["kind"] for e in out["errors"]] == ["check_not_found"]


def test_ac2_failed_log_is_a_local_artifact_and_no_github_write_happens(tmp_path: Path) -> None:
    cfg = {
        "pr_view": _pr_view(),
        "graphql": {"stdout": _gql([_node(9, "test", "COMPLETED", "FAILURE")])},
        "run_view": {"rc": 0, "stdout": "step 1 ok\nstep 2 FAILED\n"},
    }
    proc, out, calls = run_summary(tmp_path, cfg, extra=("--include-log-excerpt",))
    assert proc.returncode == 10 and out["status"] == "failed"
    assert read_only_problems(calls) == []
    assert ["run", "view"] in [c[:2] for c in calls]
    artifact = out["log_artifacts"][0]
    assert artifact["check_name"] == "test" and artifact["sha256"].startswith("sha256:")
    written = tmp_path / "work" / artifact["path"]
    assert written.is_file() and b"FAILED" in written.read_bytes()
    assert "step 2 FAILED" not in proc.stdout  # the raw log never goes to stdout


# --- AC3: schema consumer, gates, authority -------------------------------------------------


def test_ac3_approve_control_validates_against_the_real_schema_path(tmp_path: Path) -> None:
    code, out, err = run_gates(ROOT / GATES, tmp_path)
    assert (code, out["verdict"]) == (0, "APPROVE"), err
    assert gate_status(out) == {
        "g1": "pass",
        "g2": "pass",
        "g3": "not_applicable",
        "g4": "pass",
        "g5": "not_applicable",
        "g6": "pass",
    }
    assert out["schema_version"] == "PR_REVIEW_GATE_RESULT_V1"


@pytest.mark.parametrize(
    ("label", "kwargs", "gate"),
    [
        ("missing CI artifact (strict)", {"artifact": None}, "g1"),
        (
            "uncovered changed test files",
            {"artifact": {**GOOD_ARTIFACT, "uncovered_changed_test_files": ["tests/x.py"]}},
            "g1",
        ),
        ("stale head (PR vs local)", {"pr_head": "abc", "local_head": "def"}, "g4"),
        (
            "G6 allowed_paths_gate missing",
            {"body": GOOD_BODY.replace("allowed_paths_gate", "x")},
            "g6",
        ),
        ("G6 producer_role missing", {"body": GOOD_BODY.replace("producer_role", "x")}, "g6"),
        (
            "self_report_only finding without a supporting ref",
            {
                "body": "```yaml\nfindings:\n  - head_sha: abc\n    source_kind: x\n"
                "    evidence_refs:\n      self_report_only: true\n```\n"
                "allowed_paths_gate: pass\nproducer_role: review_subagent\n"
            },
            "g2",
        ),
        (
            "no structured findings",
            {"body": "plain text\nallowed_paths_gate: p\nproducer_role: r\n"},
            "g2",
        ),
    ],
)
def test_ac3_failing_gates_never_approve(
    tmp_path: Path, label: str, kwargs: dict[str, Any], gate: str
) -> None:
    code, out, err = run_gates(ROOT / GATES, tmp_path, **kwargs)
    assert gate_status(out)[gate] == "fail", (label, err)
    assert (code, out["verdict"]) == (1, "REQUEST_CHANGES"), label
    failing = next(g for g in out["gates"] if g["gate_id"] == gate)
    assert failing["minimal_context"], label


def test_ac3_aux_ci_summary_cannot_satisfy_gate_g1(tmp_path: Path) -> None:
    """An all_pass CI_VERDICT_SUMMARY_V1 is not a ci_test_selection artifact: G1 is never `pass`."""
    summary = {"schema": "CI_VERDICT_SUMMARY_V1", "status": "all_pass", "schema_version": "x"}
    _code, out, _ = run_gates(ROOT / GATES, tmp_path, artifact=summary)
    assert gate_status(out)["g1"] == "not_applicable"
    assert out["generated_by"] == "check_pr_review_gates.py"
    assert "all_pass" not in json.dumps(out)


def test_ac3_schema_validator_uses_the_real_schema_for_positive_and_negative_output() -> None:
    mod = _load(GATES, "gates_under_test_for_i4_4d6")
    schema_path = ROOT / SCHEMA
    good = {
        "schema_version": "PR_REVIEW_GATE_RESULT_V1",
        "generated_at": "2026-11-10T00:00:00Z",
        "generated_by": "check_pr_review_gates.py",
        "verdict": "APPROVE",
        "gates": [
            {"gate_id": f"g{i}", "status": "pass", "minimal_context": None} for i in range(1, 7)
        ],
    }
    assert mod.validate_against_schema(good, schema_path) == []
    bad_verdict = {**good, "verdict": "MAYBE"}
    assert any("verdict" in e for e in mod.validate_against_schema(bad_verdict, schema_path))
    missing_key = {k: v for k, v in good.items() if k != "generated_by"}
    assert any("generated_by" in e for e in mod.validate_against_schema(missing_key, schema_path))
    no_context = copy.deepcopy(good)
    no_context["gates"][0] = {"gate_id": "g1", "status": "fail", "minimal_context": None}
    assert any("minimal_context" in e for e in mod.validate_against_schema(no_context, schema_path))
    stray_context = copy.deepcopy(good)
    stray_context["gates"][0]["minimal_context"] = "x"
    assert any(
        "must not have" in e for e in mod.validate_against_schema(stray_context, schema_path)
    )
    few = {**good, "gates": good["gates"][:3]}
    assert any("at least 5" in e for e in mod.validate_against_schema(few, schema_path))
    self_report = copy.deepcopy(good)
    self_report["gates"][1] = {
        "gate_id": "g2",
        "status": "fail",
        "minimal_context": "x",
        "findings": [
            {"head_sha": "a", "source_kind": "k", "evidence_refs": {"self_report_only": True}}
        ],
    }
    assert any(
        "self_report_only" in e for e in mod.validate_against_schema(self_report, schema_path)
    )


def test_ac3_missing_or_malformed_schema_never_silently_approves(tmp_path: Path) -> None:
    text = (ROOT / GATES).read_text(encoding="utf-8")
    assert schema_fail_closed_problems(text, tmp_path) == []


def test_ac3_required_ci_authority_stays_wait_ci_checks(manifest: dict[str, Any]) -> None:
    assert authority_problems(manifest, ROOT) == []
    assert "wait_ci_checks" in (ROOT / f"{SK}/SKILL.md").read_text(encoding="utf-8")


# --- AC4: sync, boundaries, tamper detection, CI lane ----------------------------------------


def test_ac4_evidence_boundary_is_not_overclaimed(manifest: dict[str, Any]) -> None:
    assert boundary_problems(manifest, ROOT) == []


def test_ac4_history_and_residual_deferrals_are_kept() -> None:
    assert history_problems() == []


def test_ac4_d6_ci_lane_pins_the_manifest_counts(manifest: dict[str, Any]) -> None:
    assert lane_problems(CI.read_text(encoding="utf-8"), manifest) == []


def test_ac4_the_closure_test_count_is_measured_not_assumed(manifest: dict[str, Any]) -> None:
    r = _collect(THIS)
    ids = [ln for ln in r.stdout.splitlines() if "::" in ln]
    assert r.returncode == 0, r.stdout + r.stderr
    assert manifest["lane_totals"]["closure_tests_ran"] == len(ids)
    assert manifest["lane_totals"]["source_ran"] == 173


def test_ac4_source_and_edit_tampering_are_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    assert parity_problems(manifest, tmp_path) == []
    summary = tmp_path / SUMMARY
    summary.write_bytes(summary.read_bytes() + b"# tampered\n")
    assert any("mismatch" in p for p in parity_problems(manifest, tmp_path))
    shutil.copy2(ROOT / SUMMARY, summary)
    gates = tmp_path / GATES
    gates.write_text(
        gates.read_text(encoding="utf-8").replace("Schema unavailable", "Schema ok"),
        encoding="utf-8",
    )
    assert any("mismatch" in p for p in parity_problems(manifest, tmp_path))
    shutil.copy2(ROOT / GATES, gates)
    weak = copy.deepcopy(manifest)
    weak["documents"][GATES]["intentional_diff"] = []
    assert any("intentional_diff" in p for p in intentional_diff_problems(weak))
    unjustified = copy.deepcopy(manifest)
    del unjustified["documents"][T_SUMMARY]["intentional_diff"][0]["reason"]
    assert any("reason" in p for p in intentional_diff_problems(unjustified))
    extra = copy.deepcopy(manifest)
    extra["documents"][SUMMARY] = extra["verbatim_files"].pop(SUMMARY)
    assert any("edited-file set" in p for p in intentional_diff_problems(extra))


def test_ac4_the_pre_fix_gate_script_is_detected_as_fail_open(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    """Reverse the recorded edit: the source fallback exits 0 / APPROVE on a missing schema."""
    text = (ROOT / GATES).read_text(encoding="utf-8")
    for edit in manifest["documents"][GATES]["intentional_diff"]:
        assert text.count(edit["moved"]) == 1
        text = text.replace(edit["moved"], edit["source"], 1)
    problems = schema_fail_closed_problems(text, tmp_path)
    assert len(problems) == 4, problems
    assert all("not fail-closed" in p for p in problems)


def test_ac4_a_weakened_source_test_or_count_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    assert source_test_problems(manifest, ROOT) == []
    fake_root = tmp_path / "root"
    for rel in manifest["test_files"]:
        dest = fake_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("import pytest\n\n@pytest.mark.skip\ndef test_x():\n    pass\n", "utf-8")
    assert any("skip / xfail" in p for p in source_test_problems(manifest, fake_root))
    deferred = copy.deepcopy(manifest)
    deferred["deferred_tests"] = [{"id": "x"}]
    assert any("deferred" in p for p in source_test_problems(deferred, ROOT))
    miscounted = copy.deepcopy(manifest)
    miscounted["expected_collect"][T_SUMMARY] = 166
    assert any("expected_collect" in p for p in source_test_problems(miscounted, ROOT))
    short = copy.deepcopy(manifest)
    short["totals"]["ran"] = 172
    assert any("totals" in p for p in source_test_problems(short, ROOT))


def test_ac4_dependency_authority_and_boundary_tampering_are_detected(
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
    for rel in (STILL_ABSENT[0], STILL_ABSENT[-1]):
        fake = tmp_path / rel
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("# empty stub\n", encoding="utf-8")
    assert sum("present" in p for p in dependency_problems(manifest, tmp_path)) == 2
    swapped = copy.deepcopy(manifest)
    swapped["authority"]["required_ci_evaluator"] = f"{SK}/scripts/ci_verdict_summary.py"
    assert any("authority" in p for p in authority_problems(swapped, ROOT))
    for key in (
        "agent_dispatched",
        "github_mutation_by_ci_helpers",
        "disable_model_invocation_released",
    ):
        over = copy.deepcopy(manifest)
        over["evidence_boundary"][key] = True
        assert any("overclaims" in p for p in boundary_problems(over, ROOT))


def test_ac4_a_stale_absence_assertion_in_an_earlier_slice_is_detected(tmp_path: Path) -> None:
    stage = tmp_path / "tests" / "stage_i4"
    stage.mkdir(parents=True)
    d5_text = (ROOT / "tests/stage_i4/test_i4_4d5_pr_review_consumers.py").read_text("utf-8")
    (stage / "test_i4_4d5_pr_review_consumers.py").write_text(d5_text, encoding="utf-8")
    assert stale_absence_problems(tmp_path) == []
    regressed = d5_text.replace(
        "STILL_ABSENT = (", 'STILL_ABSENT = (\n    f"{SK}/scripts/ci_verdict_summary.py",', 1
    )
    (stage / "test_i4_4d5_pr_review_consumers.py").write_text(regressed, encoding="utf-8")
    assert any("stale absence" in p for p in stale_absence_problems(tmp_path))
    (stage / "test_i4_4d3_step4_step5.py").write_text(
        'assert not (ROOT / "x" / "check_pr_review_gates.py").exists()\n', encoding="utf-8"
    )
    assert len(stale_absence_problems(tmp_path)) == 2


def test_ac4_d5_history_loss_and_d5_count_tampering_are_detected(manifest: dict[str, Any]) -> None:
    d5 = _json(D5_MANIFEST)
    assert d5_sync_problems(manifest, d5) == []
    lost = copy.deepcopy(d5)
    del lost["historical_pre_d6"]
    assert any("historical_pre_d6" in p for p in d5_sync_problems(manifest, lost))
    rewritten = copy.deepcopy(d5)
    rewritten["totals"]["ran"] = 107
    assert any("108" in p for p in d5_sync_problems(manifest, rewritten))
    stale = copy.deepcopy(d5)
    stale["unmigrated_references"]["hard_dependencies"][0]["reference"] += (
        " check_pr_review_gates.py"
    )
    assert any("still lists" in p for p in d5_sync_problems(manifest, stale))


def test_ac4_ci_lane_tampering_is_detected(manifest: dict[str, Any]) -> None:
    text = CI.read_text(encoding="utf-8")
    assert lane_problems(text, manifest) == []
    assert any(
        "ran count" in p
        for p in lane_problems(
            text.replace("0 deferred with reasons, 173 ran", "0 deferred with reasons, 172 ran"),
            manifest,
        )
    )
    assert any(
        "does not run its manifest" in p
        for p in lane_problems(
            text.replace("i4-4d6-pr-review-core-manifest.json | tee", "other.json | tee"), manifest
        )
    )
    assert any(
        "reject skip" in p
        for p in lane_problems(
            text.replace("(skipped|xfailed|xpassed|deselected)", "(skipped)"), manifest
        )
    )
    assert any(
        "CI lane missing" in p for p in lane_problems(text.replace(D6_JOB, "x-job"), manifest)
    )
