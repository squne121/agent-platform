"""I-4-4D4: Skill entrypoint, main Agent definitions and read-only CI helpers (#95).

Static parity + the real source tests + a fake ``gh`` on PATH inside ``tmp_path``. Nothing here
starts an Agent or SubAgent, runs the loop, calls live GitHub, mutates a Git ref, publishes a PR,
releases the temporary ``disable-model-invocation`` or wires Final Gate / ``LOOP_HANDOFF_RESULT_V1``.

Test groups are selected by the Issue's Verification Commands with ``-k test_ac<N>_``.
"""

from __future__ import annotations

import ast
import copy
import hashlib
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
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d4-skill-agents-readonly-manifest.json"
C2_MANIFEST = DEV / "i4-4c2-intake-capsule-manifest.json"
D1_MANIFEST = DEV / "i4-4d1-preparation-step1-manifest.json"
D2_MANIFEST = DEV / "i4-4d2-step2-verification-manifest.json"
D3_MANIFEST = DEV / "i4-4d3-step4-step5-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = "tests/stage_i4/test_i4_4d4_skill_agents_readonly.py"
IRL = ".claude/skills/impl-review-loop"
TESTS = f"{IRL}/tests"
SCRIPTS = f"{IRL}/scripts"
D4_JOB = "i4-4d4-skill-agents-readonly"
LANES = {
    "i4-4c2-intake-capsule": "docs/dev/i4-4c2-intake-capsule-manifest.json",
    "i4-4d1-preparation-step1": "docs/dev/i4-4d1-preparation-step1-manifest.json",
    "i4-4d2-step2-verification": "docs/dev/i4-4d2-step2-verification-manifest.json",
    "i4-4d3-step4-step5": "docs/dev/i4-4d3-step4-step5-manifest.json",
    D4_JOB: "docs/dev/i4-4d4-skill-agents-readonly-manifest.json",
}
# Pins fixed by the Issue contract (#95): the source blobs of the Skill and the three Agents.
ISSUE_SOURCE_BLOBS = {
    f"{IRL}/SKILL.md": "7cdcffb383d69b5784ebb9166dd13a811e5e6c7c",
    ".claude/agents/implementation-worker.md": "23e76a8c1ee77ffc76e361bee23428adb77e5dda",
    ".claude/agents/test-runner.md": "4d13baf0ac1754cda4265537d3a70538329018f8",
    ".claude/agents/pr-reviewer.md": "5a83c95aebf9a4af7f1739126c569b8800af0908",
}
SOURCE_COMMIT = "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
DEFERRED_ID = (
    f"{TESTS}/test_wait_ci_checks.py::test_canonical_evaluator_consumers_use_same_function"
)
C2_RESTORED_ID = (
    f"{TESTS}/test_intake_capsule_provenance_e2e.py::test_skill_md_declares_provenance_inputs"
)
C2_CANARY_ID = (
    f"{TESTS}/test_intake_capsule_provenance_e2e.py"
    "::test_ac9_runtime_canary_known_pr_comment_resolves_against_real_github"
)
HEAD = "a" * 40
SKIP_RE = re.compile(r"pytest\.mark\.(skip|skipif|xfail)|pytest\.(skip|xfail|importorskip)\(")
# Words that would make a helper a writer; the helpers may only read.
WRITE_RE = re.compile(
    r"\b(pr (edit|merge|close|comment|review|create)|issue (edit|close|comment|create)|"
    r"git (push|commit|reset|checkout)|--method|-X\s+(POST|PUT|PATCH|DELETE))\b"
)


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


d2 = _load("tests/stage_i4/test_i4_4d2_step2_verification.py", "d2_helpers_for_i4_4d4")
document_problems = d2.document_problems
function_source = d2.function_source
job_runs = d2.job_runs


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


@pytest.fixture
def c2() -> dict[str, Any]:
    return copy.deepcopy(_json(C2_MANIFEST))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def frontmatter(path: Path) -> dict[str, Any]:
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
    assert match, path
    loaded = yaml.safe_load(match.group(1))
    assert isinstance(loaded, dict)
    return loaded


# --- problem functions (pure: they return a list of messages, [] means OK) -----------------


def parity_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Documents with recorded edits + verbatim files against the pinned source."""
    problems = document_problems(m, root)
    if not m["verbatim_files"]:
        problems.append("no verbatim files recorded")
    for rel, rec in m["verbatim_files"].items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing file: {rel}")
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != rec["moved_sha256"]:
            problems.append(f"moved_sha256 mismatch: {rel}")
        if rec["moved_sha256"] != rec["source_sha256"] or not rec["verbatim"]:
            problems.append(f"verbatim file is not recorded as identical to the source: {rel}")
        if _blob(data) != rec["source_blob_sha1"]:
            problems.append(f"source blob SHA mismatch: {rel}")
        if rel.endswith(".sh") and not os.access(path, os.X_OK):
            problems.append(f"shell wrapper lost its executable bit: {rel}")
    return problems


def issue_pin_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if m["source"]["commit"] != SOURCE_COMMIT:
        problems.append("source commit pin is not the peeled commit of the source tag")
    for rel, blob in ISSUE_SOURCE_BLOBS.items():
        if m["documents"].get(rel, {}).get("source_blob_sha1") != blob:
            problems.append(f"source blob is not the Issue-fixed pin: {rel}")
    return problems


def frontmatter_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Skill auto-invocation is off; Agent roles (read-only vs editing) and hooks are kept."""
    problems: list[str] = []
    expected = m["frontmatter"]
    skill = expected[f"{IRL}/SKILL.md"]
    skill_path = root / IRL / "SKILL.md"
    if not skill_path.is_file():
        return ["missing SKILL.md"]
    fm = frontmatter(skill_path)
    if (
        fm.get("disable-model-invocation") is not True
        or skill.get("disable-model-invocation") is not True
    ):
        problems.append("SKILL.md must carry disable-model-invocation: true (temporary)")
    if fm.get("name") != "impl-review-loop":
        problems.append("SKILL.md name changed")
    body = skill_path.read_text(encoding="utf-8")
    for needle in ("disable-model-invocation", "手動", "dispatch", "未移管"):
        if needle not in body.split("---", 2)[2]:
            problems.append(f"SKILL.md body lost the fail-closed explanation: {needle}")
    for rel, exp in expected.items():
        if rel.endswith("SKILL.md"):
            continue
        path = root / rel
        if not path.is_file():
            problems.append(f"missing Agent: {rel}")
            continue
        agent = frontmatter(path)
        for key in (
            "name",
            "tools",
            "disallowedTools",
            "model",
            "effort",
            "permissionMode",
            "skills",
        ):
            if agent.get(key) != exp.get(key):
                problems.append(f"Agent frontmatter {key} differs from the pinned source: {rel}")
        if exp.get("disallowedTools") and set(agent.get("tools") or []) & {
            "Edit",
            "Write",
            "MultiEdit",
        }:
            problems.append(f"read-only Agent gained an editing tool: {rel}")
        if "hook_commands" in exp:
            hooks = json.dumps(agent.get("hooks", {}))
            for cmd in exp["hook_commands"]:
                if cmd not in hooks:
                    problems.append(f"Agent lost its source hook declaration: {rel}")
            if set(agent.get("hooks", {})) != set(exp["hook_events"]):
                problems.append(f"Agent hook events differ from the source: {rel}")
        text = path.read_text(encoding="utf-8")
        if "I-4-4D4" not in text or "実 dispatch しない" not in text:
            problems.append(f"Agent lost its unwired / no-dispatch note: {rel}")
    tr = frontmatter(root / ".claude/agents/test-runner.md")
    pr = frontmatter(root / ".claude/agents/pr-reviewer.md")
    iw = frontmatter(root / ".claude/agents/implementation-worker.md")
    for name, fm_ in (("test-runner", tr), ("pr-reviewer", pr)):
        if not {"Edit", "Write", "MultiEdit"} <= set(fm_.get("disallowedTools") or []):
            problems.append(f"{name} lost disallowedTools Edit/Write/MultiEdit")
    if not {"Edit", "Write", "MultiEdit"} <= set(iw.get("tools") or []):
        problems.append("implementation-worker is no longer the editing role")
    return problems


def legacy_contract_problems(root: Path) -> list[str]:
    """pnpm / game workflow must not be the destination's executable contract."""
    problems: list[str] = []
    tr = (root / ".claude/agents/test-runner.md").read_text(encoding="utf-8")
    iw = (root / ".claude/agents/implementation-worker.md").read_text(encoding="utf-8")
    for label, text in (("test-runner.md", tr), ("implementation-worker.md", iw)):
        # the migration-status blockquote may name pnpm as history; commands may not
        live = "\n".join(ln for ln in text.splitlines() if not ln.startswith(">"))
        if re.search(r"\bpnpm\b", live):
            problems.append(f"{label} still contains a pnpm command")
    allowed = re.search(r"## 許可するコマンド.*?```\n(.*?)```", tr, re.DOTALL)
    if allowed is None or "uv run --locked ruff check ." not in allowed.group(1):
        problems.append("test-runner allowed commands do not name the uv / ruff contract")
    if "MovementSystem" in tr or "movement-system" in tr:
        problems.append("game-specific example left in test-runner.md")
    skill = (root / IRL / "SKILL.md").read_text(encoding="utf-8")
    head = skill.split("---", 2)[1]
    if "docs/dev/workflow.md" in head:
        problems.append("SKILL.md description still points to the legacy workflow document")
    return problems


def reference_problems(m: dict[str, Any], root: Path) -> list[str]:
    refs = m["unmigrated_references"]
    problems: list[str] = []
    if not refs["later_i_4_4d_slices"] or not refs["legacy_repository_specific"]:
        problems.append("unmigrated references are not classified")
    for entry in refs["later_i_4_4d_slices"]:
        if not (entry.get("reference") and entry.get("owner")):
            problems.append(f"unmigrated reference without owner: {entry}")
    text = "\n".join(e["reference"] for e in refs["later_i_4_4d_slices"])
    for needed in (
        "implement-issue",
        "open-pr",
        "pr-review-judge",
        "pr-reviewer-lite",
        "pr_reviewer_guard.py",
        "body_only_repair_plan.py",
        "Final Gate",
    ):
        if needed not in text:
            problems.append(f"unmigrated reference not recorded: {needed}")
    for absent in (
        ".claude/skills/implement-issue",
        ".claude/skills/open-pr",
        ".claude/skills/pr-review-judge/SKILL.md",
        ".claude/agents/pr-reviewer-lite.md",
        ".claude/hooks/pr_reviewer_guard.py",
        f"{SCRIPTS}/body_only_repair_plan.py",
    ):
        if (root / absent).exists():
            problems.append(
                f"recorded as unmigrated but present (migrate it, or update the record): {absent}"
            )
    return problems


def deferred_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Exactly the approved single deferral; the source tests themselves stay unweakened."""
    problems: list[str] = []
    deferred = m["deferred_tests"]
    if [e.get("id") for e in deferred] != [DEFERRED_ID]:
        return ["deferred_tests must be exactly the one human-approved test id"]
    entry = deferred[0]
    for key in ("reason", "owner_stage", "rerun_condition", "function", "file"):
        if not entry.get(key):
            problems.append(f"deferred entry lost {key}")
    if entry.get("kind") != "unmigrated_consumer_documents":
        problems.append("deferred entry kind changed")
    if sorted(entry.get("requires_missing_files", [])) != [
        ".claude/agents/pr-reviewer-lite.md",
        ".claude/skills/pr-review-judge/SKILL.md",
    ]:
        problems.append("deferred entry no longer names the two unmigrated consumers")
    for rel in entry.get("requires_missing_files", []):
        if (root / rel).exists():
            problems.append(f"stale deferred entry: {rel} now exists, restore {entry['id']}")
    src_file = root / entry["file"]
    body = function_source(src_file, entry["function"]) if src_file.is_file() else None
    if body is None:
        problems.append("deferred function not found in the source test file")
    else:
        for needle in (
            "pr-review-judge",
            "pr-reviewer-lite.md",
            "step-4-pr-review.md",
            "is_file()",
            "wait_ci_checks",
        ):
            if needle not in body:
                problems.append(f"deferred source test lost its assertion: {needle}")
    for rel in m["test_files"]:
        text = (root / rel).read_text(encoding="utf-8") if (root / rel).is_file() else ""
        if SKIP_RE.search(text):
            problems.append(f"skip / xfail marker in a source test file: {rel}")
    t = m["totals"]
    if t["source_collected"] != sum(m["expected_collect"].values()):
        problems.append("totals.source_collected != sum(expected_collect)")
    if t["deferred"] != len(deferred) or t["ran"] != t["source_collected"] - t["deferred"]:
        problems.append("totals.deferred / ran inconsistent")
    if t["ran"] != sum(m["expected_ran"].values()):
        problems.append("totals.ran != sum(expected_ran)")
    if (
        (t["source_collected"], t["deferred"], t["ran"]) != (108, 1, 107)
        or t["skipped"]
        or t["xfailed"]
    ):
        problems.append("D4 source totals are not 108 collected / 1 deferred / 107 ran / 0 skipped")
    return problems


def c2_problems(m: dict[str, Any], c2_manifest: dict[str, Any]) -> list[str]:
    """C2 restored the SKILL.md test, keeps the live canary and both historical measurements."""
    problems: list[str] = []
    rec = m["c2_restoration"]
    ids = {e["id"] for e in c2_manifest["deferred_tests"]}
    source_ids = {e["id"] for e in c2_manifest["deferred_source_tests"]}
    if C2_RESTORED_ID in ids or C2_RESTORED_ID in source_ids:
        problems.append("the SKILL.md test is still deferred in C2")
    if ids != {C2_CANARY_ID} or source_ids != {C2_CANARY_ID}:
        problems.append("C2 must defer exactly the live GitHub canary")
    for e in c2_manifest["deferred_tests"]:
        for key in ("reason", "owner_stage", "rerun_condition"):
            if not e.get(key):
                problems.append(f"live canary lost {key}")
    t = c2_manifest["totals"]
    cur = rec["current"]
    if (t["source_collected"], t["ran"], t["deferred"], t["skipped"]) != (
        cur["source_collected"],
        cur["ran"],
        cur["deferred"],
        cur["skipped"],
    ) or (cur["ran"], cur["deferred"]) != (118, 1):
        problems.append("C2 totals are not the measured 118 ran / 1 deferred")
    if sum(c2_manifest["expected_ran"].values()) != t["ran"]:
        problems.append("C2 expected_ran does not sum to totals.ran")
    pre_d4 = c2_manifest.get("historical_pre_d4", {}).get("totals", {})
    if (pre_d4.get("source_collected"), pre_d4.get("ran"), pre_d4.get("deferred")) != (119, 117, 2):
        problems.append("the D1-time C2 measurement (119 / 117 / 2) was lost or rewritten")
    pre_d1 = c2_manifest.get("historical_pre_d1", {}).get("totals", {})
    if (pre_d1.get("source_collected"), pre_d1.get("ran"), pre_d1.get("deferred")) != (119, 112, 7):
        problems.append("the original C2 measurement (119 / 112 / 7) was lost or rewritten")
    for key in ("historical_pre_d4", "historical_pre_d1"):
        h = rec[key]
        want = (119, 117, 2) if key.endswith("d4") else (119, 112, 7)
        if (h["source_collected"], h["ran"], h["deferred"]) != want:
            problems.append(f"D4 record of {key} is wrong")
    if c2_manifest.get("restored_by_d4", {}).get("restored_tests") != [C2_RESTORED_ID]:
        problems.append("C2 restored_by_d4 does not name the SKILL.md test")
    return problems


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    """Every lane pins its manifest ran count; the D4 lane also pins the single deselect."""
    problems: list[str] = []
    pins = {
        "i4-4c2-intake-capsule": (1, _json(C2_MANIFEST)["totals"]["ran"]),
        "i4-4d1-preparation-step1": (0, _json(D1_MANIFEST)["totals"]["ran"]),
        "i4-4d2-step2-verification": (0, _json(D2_MANIFEST)["totals"]["ran"]),
        "i4-4d3-step4-step5": (0, _json(D3_MANIFEST)["totals"]["ran"]),
        D4_JOB: (m["totals"]["deferred"], m["totals"]["ran"]),
    }
    for job, (deferred, ran) in pins.items():
        runs = job_runs(ci_text, job)
        if runs is None:
            problems.append(f"CI lane missing: {job}")
            continue
        if f"run_stage1b1_tests.py {LANES[job]}" not in runs:
            problems.append(f"CI lane does not run its manifest: {job}")
        if f"{deferred} deferred with reasons, {ran} ran, 0 skipped" not in runs:
            problems.append(f"CI lane does not pin the manifest ran count: {job}")
        if "tests/stage_i4" not in runs:
            problems.append(f"CI lane does not run the closure tests: {job}")
        if "(skipped|xfailed|xpassed" not in runs and "[0-9]+ (skipped|xfailed" not in runs:
            problems.append(f"CI lane does not reject skip/xfail: {job}")
        if deferred and f"{deferred} deselected" not in runs:
            problems.append(f"CI lane does not pin the deselect count: {job}")
    return problems


def stale_absence_problems(root: Path) -> list[str]:
    """D1/D2/D3 must no longer assert the absence of what D4 migrated."""
    problems: list[str] = []
    stale = re.compile(
        r"^\s*assert not .*(\"SKILL\.md\"|test-runner\.md|pr-reviewer\.md|implementation-worker\.md"
        r"|wait_ci_checks|get_ci_failed_log|context-protocol-and-guardrails"
        r"|conflicting-pr-escalation-runbook)"
    )
    in_tuple = re.compile(
        r"^\s*(irl / \"SKILL\.md\"|.*agents.*(test-runner|pr-reviewer|implementation-worker)\.md.*),\s*$"
    )
    for rel in (
        "tests/stage_i4/test_i4_4d1_preparation_step1.py",
        "tests/stage_i4/test_i4_4d2_step2_verification.py",
        "tests/stage_i4/test_i4_4d3_step4_step5.py",
    ):
        lines = (root / rel).read_text(encoding="utf-8").splitlines()
        absent_block = False
        for number, line in enumerate(lines, 1):
            if "for absent in (" in line:
                absent_block = True
            elif absent_block and line.strip().startswith("):"):
                absent_block = False
            if stale.search(line) or (absent_block and in_tuple.search(line)):
                problems.append(f"stale absence assertion: {rel}:{number}")
    return problems


def readonly_problems(root: Path) -> list[str]:
    """The helpers only call read-only gh subcommands (static)."""
    problems: list[str] = []
    for name in (
        "wait_ci_checks.py",
        "get_ci_failed_log.py",
        "wait_ci_checks.sh",
        "get_ci_failed_log.sh",
    ):
        text = (root / SCRIPTS / name).read_text(encoding="utf-8")
        if WRITE_RE.search(text):
            problems.append(f"write-capable command in a read-only helper: {name}")
    for name in ("wait_ci_checks.py", "get_ci_failed_log.py"):
        tree = ast.parse((root / SCRIPTS / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_gh":
                first = [a.value for a in node.args if isinstance(a, ast.Constant)][:2]
                if first and first[0] not in {"pr", "run", "api"}:
                    problems.append(f"unexpected gh subcommand in {name}: {first}")
                if first and first[0] == "pr" and first[1:2] and first[1] not in {"view", "checks"}:
                    problems.append(f"non-read gh pr subcommand in {name}: {first}")
                if first and first[0] == "run" and first[1:2] and first[1] not in {"view", "list"}:
                    problems.append(f"non-read gh run subcommand in {name}: {first}")
    return problems


# --- fake gh (real helper scripts, a stand-in gh binary on PATH, tmp_path only) -----------

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
scenario = json.load(open(os.environ["FAKE_GH_SCENARIO"]))
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
argv = sys.argv[1:]
def out(rc=0, stdout="", stderr=""):
    sys.stdout.write(stdout); sys.stderr.write(stderr); sys.exit(rc)
def reply(key):
    r = scenario[key]
    out(r.get("rc", 0), r.get("stdout", ""), r.get("stderr", ""))
if argv[:2] == ["pr", "checks"]:
    reply("checks")
if argv[:2] == ["pr", "view"] and "baseRefName" in argv:
    reply("base")
if argv[:2] == ["pr", "view"]:
    reply("head")
if argv[:1] == ["api"] and "rules/branches" in argv[1]:
    reply("rules")
if argv[:1] == ["api"] and "required_status_checks" in argv[1]:
    reply("classic")
if argv[:2] == ["run", "list"]:
    reply("run_list")
if argv[:2] == ["run", "view"] and "--log-failed" in argv:
    reply("log_failed")
if argv[:2] == ["run", "view"]:
    reply("run_view")
if argv[:1] == ["api"] and argv[1].endswith("/jobs"):
    reply("jobs")
if argv[:1] == ["api"] and argv[1].endswith("/logs"):
    reply("job_logs")
out(97, "", "unexpected gh invocation: %r" % argv)
"""


def _fake_env(tmp_path: Path, scenario: dict[str, Any]) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    (tmp_path / "gh.log").write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        FAKE_GH_SCENARIO=str(tmp_path / "scenario.json"),
        FAKE_GH_LOG=str(tmp_path / "gh.log"),
    )
    return env


def _ok(stdout: str = "") -> dict[str, Any]:
    return {"rc": 0, "stdout": stdout}


def _wait_scenario(buckets: list[str] | None, *, head: str = HEAD, **over: Any) -> dict[str, Any]:
    checks = (
        [] if buckets is None else [{"name": f"c{i}", "bucket": b} for i, b in enumerate(buckets)]
    )
    scenario = {
        "checks": _ok(json.dumps(checks)),
        "head": _ok(head + "\n"),
        "base": _ok("main\n"),
        "rules": _ok("[[]]"),
        "classic": {"rc": 1, "stderr": "Branch not protected"},
    }
    scenario.update(over)
    return scenario


def run_wait(tmp_path: Path, scenario: dict[str, Any], *, via: str = "sh", timeout: int = 1):
    env = _fake_env(tmp_path, scenario)
    exe = (
        [str(ROOT / SCRIPTS / "wait_ci_checks.sh")]
        if via == "sh"
        else [sys.executable, str(ROOT / SCRIPTS / "wait_ci_checks.py")]
    )
    r = subprocess.run(
        [
            *exe,
            "--repo",
            "o/r",
            "--pr",
            "7",
            "--head-sha",
            HEAD,
            "--required",
            "--interval",
            "1",
            "--timeout-seconds",
            str(timeout),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
        timeout=60,
    )
    marker = next(
        (ln for ln in r.stdout.splitlines() if ln.startswith("CI_WAIT_RESULT_V1_JSON=")), None
    )
    assert marker is not None, r.stdout + r.stderr
    return r.returncode, json.loads(marker.split("=", 1)[1]), tmp_path / "gh.log"


def run_log(tmp_path: Path, scenario: dict[str, Any], *extra: str):
    env = _fake_env(tmp_path, scenario)
    r = subprocess.run(
        [
            str(ROOT / SCRIPTS / "get_ci_failed_log.sh"),
            "--repo",
            "o/r",
            "--pr",
            "7",
            "--head-sha",
            HEAD,
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
        timeout=60,
    )
    marker = next(
        (ln for ln in r.stdout.splitlines() if ln.startswith("CI_FAILED_LOG_RESULT_V1_JSON:")), None
    )
    assert marker is not None, r.stdout + r.stderr
    return r.returncode, json.loads(marker.split(":", 1)[1]), r.stdout, tmp_path / "gh.log"


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


# --- AC1: source parity, pins, frontmatter and boundaries --------------------------------


def test_ac1_files_reproduce_the_pinned_source(manifest: dict[str, Any]) -> None:
    assert parity_problems(manifest, ROOT) == []
    assert issue_pin_problems(manifest) == []
    assert list(manifest["documents"])[:4] == list(ISSUE_SOURCE_BLOBS) or set(
        ISSUE_SOURCE_BLOBS
    ) <= set(manifest["documents"])
    # every Allowed Path file of the slice is accounted for exactly once
    migrated = {*manifest["documents"], *manifest["verbatim_files"]}
    assert len(migrated) == 14
    assert {
        f"{SCRIPTS}/{n}"
        for n in (
            "wait_ci_checks.py",
            "wait_ci_checks.sh",
            "get_ci_failed_log.py",
            "get_ci_failed_log.sh",
        )
    } <= migrated


def test_ac1_skill_blocks_auto_invocation_and_agents_keep_their_roles(
    manifest: dict[str, Any],
) -> None:
    assert frontmatter_problems(manifest, ROOT) == []
    skill = frontmatter(ROOT / IRL / "SKILL.md")
    assert skill["disable-model-invocation"] is True
    # the unwired state is stated in every migrated Agent / runbook, not only in the manifest
    for rel in (
        ".claude/agents/implementation-worker.md",
        ".claude/agents/test-runner.md",
        ".claude/agents/pr-reviewer.md",
        f"{IRL}/steps/context-protocol-and-guardrails.md",
        f"{IRL}/steps/conflicting-pr-escalation-runbook.md",
    ):
        assert "移管状況 (I-4-4D4)" in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_ac1_pr_reviewer_keeps_hooks_and_preload_but_is_never_dispatched(
    manifest: dict[str, Any],
) -> None:
    fm = frontmatter(ROOT / ".claude/agents/pr-reviewer.md")
    assert fm["skills"] == ["pr-review-judge"]
    assert set(fm["hooks"]) == {"PreToolUse", "SessionStart", "PostToolUse"}
    assert "pr_reviewer_guard.py" in json.dumps(fm["hooks"])
    # the hook file / Skill are not migrated: the record must say protection is not effective
    assert not (ROOT / ".claude/hooks/pr_reviewer_guard.py").exists()
    assert manifest["evidence_boundary"]["hook_effective_protection"] is False
    assert "実効" in (ROOT / ".claude/agents/pr-reviewer.md").read_text(encoding="utf-8")


def test_ac1_legacy_pnpm_and_game_contracts_are_not_the_destination_contract() -> None:
    assert legacy_contract_problems(ROOT) == []


def test_ac1_unmigrated_references_have_an_owner_and_are_really_absent(
    manifest: dict[str, Any],
) -> None:
    assert reference_problems(manifest, ROOT) == []


def test_ac1_evidence_boundary_is_not_overclaimed(manifest: dict[str, Any]) -> None:
    boundary = manifest["evidence_boundary"]
    for key in (
        "live_e2e",
        "agent_dispatched",
        "skill_production_invocable",
        "final_gate_wired",
        "loop_handoff_result_v1_wired",
        "github_live_operations",
        "git_ref_mutation",
        "hook_effective_protection",
    ):
        assert boundary[key] is False, key
    text = json.dumps(manifest["decisions"])
    assert "temporary" in text and "never dispatches" in text


# --- AC2: real source tests, fake gh fail-closed negatives, report contract ---------------


def test_ac2_source_tests_run_107_pass_and_the_single_deferral_is_not_counted(
    manifest: dict[str, Any],
) -> None:
    assert deferred_problems(manifest, ROOT) == []
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            "scripts/ci/run_stage1b1_tests.py",
            "docs/dev/i4-4d4-skill-agents-readonly-manifest.json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "108 collected, 1 deferred with reasons, 107 ran, 0 skipped" in out, out
    assert "107 passed, 1 deselected" in out, out
    assert not any(w in out for w in ("xfailed", "xpassed", "skipped,")), out


def test_ac2_the_deferred_test_is_not_green_while_its_consumers_are_missing() -> None:
    """Running it for real fails on the unmigrated consumer: the deferral hides nothing."""
    r = _pytest(DEFERRED_ID)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "pr-review-judge/SKILL.md not found" in out, out


@pytest.mark.parametrize("via", ["sh", "py"])
def test_ac2_wait_ci_checks_passes_only_for_real_required_checks(tmp_path: Path, via: str) -> None:
    rc, result, _log = run_wait(tmp_path, _wait_scenario(["pass", "pass"]), via=via)
    assert (rc, result["status"]) == (0, "passed")
    assert result["required_only"] is True and result["head_sha"] == HEAD


@pytest.mark.parametrize(
    ("name", "scenario", "status", "code"),
    [
        ("failed", _wait_scenario(["pass", "fail"]), "failed", 1),
        ("cancelled", _wait_scenario(["cancel"]), "cancelled", 1),
        ("skipped_only", _wait_scenario(["skipping", "skipping"]), "skipped_only", 1),
        ("skipped_mixed", _wait_scenario(["pass", "skipping"]), "failed", 1),
        ("stale_head", _wait_scenario(["pass"], head="b" * 40), "head_sha_changed", 1),
        ("no_checks", _wait_scenario(None, checks={"rc": 0, "stdout": "[]"}), "no_checks", 1),
        (
            "auth",
            _wait_scenario(
                ["pass"], checks={"rc": 1, "stderr": "gh: not logged in; run gh auth login"}
            ),
            "auth_error",
            2,
        ),
        ("malformed", _wait_scenario(["pass"], checks=_ok("not json")), "malformed_gh_response", 2),
        ("not_a_list", _wait_scenario(["pass"], checks=_ok('{"a":1}')), "malformed_gh_response", 2),
    ],
)
def test_ac2_wait_ci_checks_fails_closed_with_the_real_script(
    tmp_path: Path, name: str, scenario: dict[str, Any], status: str, code: int
) -> None:
    rc, result, _log = run_wait(tmp_path, scenario)
    assert (rc, result["status"]) == (code, status), name
    assert result["status"] != "passed"


def test_ac2_wait_ci_checks_missing_required_context_never_passes(tmp_path: Path) -> None:
    """A required context from branch protection that never reports is pending, then timed out."""
    scenario = _wait_scenario(
        ["pass"],
        classic=_ok(
            json.dumps(
                {
                    "checks": [
                        {"context": "c0", "app_id": 1},
                        {"context": "missing-required", "app_id": 1},
                    ],
                    "contexts": [],
                }
            )
        ),
    )
    rc, result, _log = run_wait(tmp_path, scenario, timeout=2)
    assert (rc, result["status"]) == (1, "pending_timeout")
    assert "missing-required" in (result["message"] or "")


def test_ac2_wait_ci_checks_requires_the_required_flag(tmp_path: Path) -> None:
    env = _fake_env(tmp_path, _wait_scenario(["pass"]))
    r = subprocess.run(
        [
            str(ROOT / SCRIPTS / "wait_ci_checks.sh"),
            "--repo",
            "o/r",
            "--pr",
            "7",
            "--head-sha",
            HEAD,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
        timeout=60,
    )
    assert r.returncode != 0
    assert (tmp_path / "gh.log").read_text() == ""  # refused before any gh call


def _log_scenario(**over: Any) -> dict[str, Any]:
    run = {
        "databaseId": 11,
        "attempt": 1,
        "status": "completed",
        "conclusion": "failure",
        "headSha": HEAD,
        "workflowName": "ci",
    }
    scenario = {
        "run_list": _ok(json.dumps([run])),
        "run_view": _ok(json.dumps(run)),
        "jobs": _ok(json.dumps({"id": 5, "name": "unit", "conclusion": "failure"}) + "\n"),
        "log_failed": _ok("boom ghp_" + "a" * 36 + "\n"),
        "job_logs": _ok("rest log\n"),
    }
    scenario.update(over)
    return scenario


def test_ac2_get_ci_failed_log_reads_and_redacts_with_the_real_script(tmp_path: Path) -> None:
    rc, marker, stdout, _log = run_log(tmp_path, _log_scenario())
    assert (rc, marker["status"]) == (1, "ci_failed")
    assert marker["redaction_applied"] is True and "ghp_" not in stdout
    assert marker["failed_jobs"] and marker["retrieval_method"] == "gh_log_failed"


@pytest.mark.parametrize(
    ("scenario", "args", "status", "code"),
    [
        (_log_scenario(run_list=_ok("[]")), (), "no_matching_run", 1),
        (
            _log_scenario(run_list={"rc": 1, "stderr": "authentication required: gh auth login"}),
            (),
            "auth_error",
            2,
        ),
        (_log_scenario(run_list=_ok("nope")), (), "malformed_gh_response", 2),
        (
            _log_scenario(run_view=_ok(json.dumps({"headSha": "c" * 40, "workflowName": "ci"}))),
            ("--run-id", "11"),
            "head_sha_mismatch",
            1,
        ),
        (_log_scenario(log_failed={"rc": 1}, job_logs={"rc": 1}), (), "log_unavailable", 1),
    ],
)
def test_ac2_get_ci_failed_log_fails_closed_with_the_real_script(
    tmp_path: Path, scenario: dict[str, Any], args: tuple[str, ...], status: str, code: int
) -> None:
    rc, marker, _stdout, _log = run_log(tmp_path, scenario, *args)
    assert (rc, marker["status"]) == (code, status)


def test_ac2_get_ci_failed_log_pending_and_passed_are_not_failures(tmp_path: Path) -> None:
    pending = {
        "databaseId": 11,
        "attempt": 1,
        "status": "in_progress",
        "conclusion": None,
        "headSha": HEAD,
        "workflowName": "ci",
    }
    rc, marker, _, _ = run_log(tmp_path, _log_scenario(run_list=_ok(json.dumps([pending]))))
    assert (rc, marker["status"]) == (0, "ci_pending")
    passed = dict(pending, status="completed", conclusion="success")
    rc, marker, _, _ = run_log(tmp_path, _log_scenario(run_list=_ok(json.dumps([passed]))))
    assert (rc, marker["status"]) == (0, "ci_passed")


def test_ac2_the_helpers_only_ever_called_read_only_gh_commands(tmp_path: Path) -> None:
    run_wait(tmp_path, _wait_scenario(["pass"]))
    run_log(tmp_path, _log_scenario())
    calls = [json.loads(ln) for ln in (tmp_path / "gh.log").read_text().splitlines()]
    assert calls
    for argv in calls:
        assert argv[0] in {"pr", "run", "api"}, argv
        if argv[0] == "pr":
            assert argv[1] in {"view", "checks"}, argv
        if argv[0] == "run":
            assert argv[1] in {"view", "list"}, argv
        assert "--method" not in argv and "-X" not in argv, argv
    assert readonly_problems(ROOT) == []


def test_ac2_test_runner_report_example_matches_the_existing_adjudicator() -> None:
    r = _pytest(f"{TESTS}/test_test_runner_report_contract.py")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "8 passed" in out and "skipped" not in out and "deselected" not in out, out
    assert (ROOT / SCRIPTS / "adjudicate_vc_result.py").is_file()


# --- AC3: C2 restoration, counts and history ----------------------------------------------


def test_ac3_c2_skill_md_test_is_restored_and_passes() -> None:
    r = _pytest(C2_RESTORED_ID)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "1 passed" in out, out
    assert not any(w in out for w in ("skipped", "xfailed", "deselected")), out


def test_ac3_c2_is_118_ran_1_deferred_with_both_histories(
    manifest: dict[str, Any], c2: dict[str, Any]
) -> None:
    assert c2_problems(manifest, c2) == []
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            "scripts/ci/run_stage1b1_tests.py",
            "docs/dev/i4-4c2-intake-capsule-manifest.json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "119 collected, 1 deferred with reasons, 118 ran, 0 skipped" in out, out


def test_ac3_the_live_canary_is_still_deferred_with_reason_owner_and_rerun(
    c2: dict[str, Any],
) -> None:
    (entry,) = c2["deferred_tests"]
    assert entry["id"] == C2_CANARY_ID and entry["kind"] == "live_github_canary"
    assert entry["owner_stage"] == "I-4-4D"
    assert "fixture" in entry["reason"] and "live" in entry["rerun_condition"]


def test_ac3_all_ci_lanes_pin_the_current_manifest_counts(manifest: dict[str, Any]) -> None:
    assert lane_problems(CI.read_text(encoding="utf-8"), manifest) == []


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


# --- AC4: the stale absence assertions are resolved; negative controls ---------------------


def test_ac4_no_stale_absence_assertion_remains_in_d1_d2_d3() -> None:
    assert stale_absence_problems(ROOT) == []


def test_ac4_stale_absence_assertion_is_detected(tmp_path: Path) -> None:
    for rel in (
        "tests/stage_i4/test_i4_4d1_preparation_step1.py",
        "tests/stage_i4/test_i4_4d2_step2_verification.py",
        "tests/stage_i4/test_i4_4d3_step4_step5.py",
    ):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    assert stale_absence_problems(tmp_path) == []
    d1 = tmp_path / "tests/stage_i4/test_i4_4d1_preparation_step1.py"
    d1.write_text(
        d1.read_text() + '\n    assert not (irl / "SKILL.md").exists()\n', encoding="utf-8"
    )
    assert any("stale absence" in p for p in stale_absence_problems(tmp_path))
    d3 = tmp_path / "tests/stage_i4/test_i4_4d3_step4_step5.py"
    d3.write_text(
        d3.read_text()
        + '\n    for absent in (\n        ROOT / ".claude" / "agents" / "test-runner.md",\n    ):\n        assert not absent.exists()\n',
        encoding="utf-8",
    )
    assert len(stale_absence_problems(tmp_path)) >= 2


def _copy_tree(manifest: dict[str, Any], tmp_path: Path) -> None:
    for rel in [*manifest["documents"], *manifest["verbatim_files"], *manifest["test_files"]]:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)


def test_ac4_missing_or_tampered_file_and_source_pin_are_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    assert any("missing" in p for p in parity_problems(manifest, tmp_path))
    _copy_tree(manifest, tmp_path)
    assert parity_problems(manifest, tmp_path) == []
    helper = tmp_path / SCRIPTS / "wait_ci_checks.py"
    helper.write_text(helper.read_text() + "\n# weakened\n", encoding="utf-8")
    assert any("moved_sha256 mismatch" in p for p in parity_problems(manifest, tmp_path))
    helper.unlink()  # a missing helper script
    assert any("missing file" in p for p in parity_problems(manifest, tmp_path))
    wrong = copy.deepcopy(manifest)
    first = next(iter(wrong["documents"]))
    wrong["documents"][first]["source_blob_sha1"] = "0" * 40
    assert any("source blob SHA" in p for p in parity_problems(wrong, ROOT))
    assert any("Issue-fixed pin" in p for p in issue_pin_problems(wrong))
    wrong_commit = copy.deepcopy(manifest)
    wrong_commit["source"]["commit"] = "0" * 40
    assert any("source commit pin" in p for p in parity_problems(wrong_commit, ROOT))
    hollow = copy.deepcopy(manifest)
    hollow["documents"][first]["intentional_diff"] = []
    assert any("intentional_diff not recorded" in p for p in parity_problems(hollow, ROOT))


def test_ac4_a_weakened_source_test_assertion_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    test_file = tmp_path / TESTS / "test_wait_ci_checks.py"
    text = test_file.read_text(encoding="utf-8")
    test_file.write_text(
        text.replace(
            '"pr-reviewer-lite.md": REPO_ROOT / ".claude" / "agents" / "pr-reviewer-lite.md",', ""
        ),
        encoding="utf-8",
    )
    assert any("moved_sha256 mismatch" in p for p in parity_problems(manifest, tmp_path))
    assert any("lost its assertion" in p for p in deferred_problems(manifest, tmp_path))
    test_file.write_text(text + "\n@pytest.mark.skip\ndef test_x():\n    pass\n", encoding="utf-8")
    assert any("skip / xfail marker" in p for p in deferred_problems(manifest, tmp_path))


def test_ac4_false_deferred_and_count_tampering_are_detected(manifest: dict[str, Any]) -> None:
    extra = copy.deepcopy(manifest)
    other = copy.deepcopy(extra["deferred_tests"][0])
    other["id"] = f"{TESTS}/test_get_ci_failed_log.py::test_anything"
    extra["deferred_tests"].append(other)
    assert any("exactly the one human-approved" in p for p in deferred_problems(extra, ROOT))
    dropped = copy.deepcopy(manifest)
    dropped["deferred_tests"] = []
    assert deferred_problems(dropped, ROOT)
    swapped = copy.deepcopy(manifest)
    swapped["deferred_tests"][0]["id"] = (
        f"{TESTS}/test_wait_ci_checks.py::test_passed_required_checks"
    )
    assert deferred_problems(swapped, ROOT)
    for key in ("reason", "owner_stage", "rerun_condition"):
        broken = copy.deepcopy(manifest)
        broken["deferred_tests"][0][key] = ""
        assert any(f"lost {key}" in p for p in deferred_problems(broken, ROOT))
    counts = copy.deepcopy(manifest)
    counts["totals"]["ran"] = 108
    assert deferred_problems(counts, ROOT)
    counts = copy.deepcopy(manifest)
    counts["expected_ran"][f"{TESTS}/test_get_ci_failed_log.py"] += 1
    assert deferred_problems(counts, ROOT)


def test_ac4_a_deferral_whose_consumers_now_exist_is_stale(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    for rel in manifest["deferred_tests"][0]["requires_missing_files"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("wait_ci_checks\n", encoding="utf-8")
    assert any("stale deferred entry" in p for p in deferred_problems(manifest, tmp_path))


def test_ac4_c2_history_and_restoration_tampering_are_detected(
    manifest: dict[str, Any], c2: dict[str, Any]
) -> None:
    undone = copy.deepcopy(c2)
    entry = {
        "id": C2_RESTORED_ID,
        "kind": "unmigrated_document",
        "reason": "x",
        "owner_stage": "x",
        "rerun_condition": "x",
    }
    undone["deferred_tests"].insert(0, copy.deepcopy(entry))
    undone["deferred_source_tests"].insert(0, copy.deepcopy(entry))
    assert any("still deferred in C2" in p for p in c2_problems(manifest, undone))
    for key, bad in (("historical_pre_d4", 118), ("historical_pre_d1", 100)):
        rewritten = copy.deepcopy(c2)
        rewritten[key]["totals"]["ran"] = bad
        assert any("was lost or rewritten" in p for p in c2_problems(manifest, rewritten))
    totals = copy.deepcopy(c2)
    totals["totals"]["ran"] = 117
    assert any("118 ran / 1 deferred" in p for p in c2_problems(manifest, totals))
    no_canary = copy.deepcopy(c2)
    no_canary["deferred_tests"] = []
    no_canary["deferred_source_tests"] = []
    assert any("live GitHub canary" in p for p in c2_problems(manifest, no_canary))
    reasonless = copy.deepcopy(c2)
    reasonless["deferred_tests"][0]["owner_stage"] = ""
    assert any("lost owner_stage" in p for p in c2_problems(manifest, reasonless))


def test_ac4_ci_lane_tampering_is_detected(manifest: dict[str, Any]) -> None:
    text = CI.read_text(encoding="utf-8")
    assert lane_problems(text, manifest) == []
    assert any(
        "ran count" in p
        for p in lane_problems(
            text.replace("1 deferred with reasons, 107 ran", "1 deferred with reasons, 108 ran"),
            manifest,
        )
    )
    assert any(
        "ran count" in p
        for p in lane_problems(
            text.replace("1 deferred with reasons, 118 ran", "1 deferred with reasons, 117 ran"),
            manifest,
        )
    )
    assert any(
        "deselect count" in p
        for p in lane_problems(
            text.replace("(^|[^0-9])1 deselected", "(^|[^0-9])2 deselected"), manifest
        )
    )
    assert any(
        "does not run its manifest" in p
        for p in lane_problems(
            text.replace("i4-4d4-skill-agents-readonly-manifest.json | tee", "other.json | tee"),
            manifest,
        )
    )
    assert any(
        "lane missing" in p
        for p in lane_problems(text.replace(f"  {D4_JOB}:", "  renamed-job:"), manifest)
    )


def test_ac4_auto_invocation_unwired_note_and_agent_boundary_tampering_are_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    assert frontmatter_problems(manifest, tmp_path) == []
    skill = tmp_path / IRL / "SKILL.md"
    original = skill.read_text(encoding="utf-8")
    skill.write_text(original.replace("disable-model-invocation: true\n", "", 1), encoding="utf-8")
    assert any("disable-model-invocation" in p for p in frontmatter_problems(manifest, tmp_path))
    skill.write_text(original, encoding="utf-8")
    runner = tmp_path / ".claude/agents/test-runner.md"
    text = runner.read_text(encoding="utf-8")
    runner.write_text(
        text.replace("disallowedTools:\n  - Edit\n  - Write\n  - MultiEdit\n", "", 1),
        encoding="utf-8",
    )
    assert any(
        "test-runner" in p and "disallowedTools" in p
        for p in frontmatter_problems(manifest, tmp_path)
    )
    runner.write_text(text.replace("I-4-4D4", "I-4-4DX"), encoding="utf-8")
    assert any("unwired / no-dispatch note" in p for p in frontmatter_problems(manifest, tmp_path))
    runner.write_text(text, encoding="utf-8")
    reviewer = tmp_path / ".claude/agents/pr-reviewer.md"
    rtext = reviewer.read_text(encoding="utf-8")
    reviewer.write_text(
        re.sub(r"\nhooks:\n(?:  .*\n|    .*\n)+", "\n", rtext, count=1), encoding="utf-8"
    )
    assert any("hook" in p for p in frontmatter_problems(manifest, tmp_path))
    reviewer.write_text(rtext.replace("skills:\n  - pr-review-judge\n", "", 1), encoding="utf-8")
    assert any(
        "differs from the pinned source" in p for p in frontmatter_problems(manifest, tmp_path)
    )


def test_ac4_legacy_contract_and_unmigrated_record_tampering_are_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    _copy_tree(manifest, tmp_path)
    assert legacy_contract_problems(tmp_path) == []
    runner = tmp_path / ".claude/agents/test-runner.md"
    runner.write_text(runner.read_text() + "\npnpm test\n", encoding="utf-8")
    assert any("pnpm" in p for p in legacy_contract_problems(tmp_path))
    ownerless = copy.deepcopy(manifest)
    ownerless["unmigrated_references"]["later_i_4_4d_slices"][0]["owner"] = ""
    assert any("without owner" in p for p in reference_problems(ownerless, ROOT))
    forgotten = copy.deepcopy(manifest)
    forgotten["unmigrated_references"]["later_i_4_4d_slices"] = forgotten["unmigrated_references"][
        "later_i_4_4d_slices"
    ][:2]
    assert any("not recorded" in p for p in reference_problems(forgotten, ROOT))
    faked = tmp_path / ".claude/skills/pr-review-judge/SKILL.md"
    faked.parent.mkdir(parents=True, exist_ok=True)
    faked.write_text("placeholder\n", encoding="utf-8")
    assert any(
        "recorded as unmigrated but present" in p for p in reference_problems(manifest, tmp_path)
    )


def test_ac4_a_write_capable_helper_is_detected(manifest: dict[str, Any], tmp_path: Path) -> None:
    _copy_tree(manifest, tmp_path)
    assert readonly_problems(tmp_path) == []
    helper = tmp_path / SCRIPTS / "get_ci_failed_log.py"
    helper.write_text(helper.read_text() + '\nrun_gh("pr", "merge", "7")\n', encoding="utf-8")
    problems = readonly_problems(tmp_path)
    assert any("get_ci_failed_log.py" in p for p in problems)


def test_ac4_historical_manifests_are_not_rewritten() -> None:
    d1, d3 = _json(D1_MANIFEST), _json(D3_MANIFEST)
    assert (d1["c2_counts"]["current"]["ran"], d1["c2_counts"]["current"]["deferred"]) == (117, 2)
    assert (
        d1["c2_counts"]["historical_pre_d1"]["ran"],
        d1["c2_counts"]["historical_pre_d1"]["deferred"],
    ) == (112, 7)
    assert "note_i_4_4d4" in d1["c2_counts"] and "note_i_4_4d4" in d3["unmigrated_references"]
    assert d3["unmigrated_references"]["later_i_4_4d_slices"], "the D3-time record stays as history"
