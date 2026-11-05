"""I-4-4C3: TEST_VERDICT artifact materializer + source tests closure (#85).

Fixture / fake-gh evidence only. Nothing here calls live GitHub, calls or enables
`test_verdict.publish`, or touches Final Gate / `LOOP_HANDOFF_RESULT_V1`.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-4c3-test-verdict-artifact-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
IRL = ".claude/skills/impl-review-loop"
MATERIALIZER = f"{IRL}/scripts/materialize_test_verdict_artifact.py"
EXECUTOR = "scripts/agent-guards/controlled_skill_mutation_exec.py"
CI_JOB = "i4-4c3-test-verdict-artifact"
RUNNER_ARGS = [
    "scripts/ci/run_stage1b1_tests.py",
    "docs/dev/i4-4c3-test-verdict-artifact-manifest.json",
]
SKIP_NAMES = re.compile(r"^pytest\.(mark\.(skip|skipif|xfail)|skip|xfail|importorskip)$")
# Reused from the existing controlled executor (Child B); the materializer must not redefine them.
SHARED_FUNCTIONS = (
    "_validate_test_verdict_publish_fields",
    "_verify_producer_run_and_job",
    "_verify_execution_artifact_metadata",
    "_download_and_verify_artifact_archive",
    "_find_gh_bin",
    "_build_pr_review_gh_env",
)
FORBIDDEN_IMPORTS = {"subprocess", "socket", "urllib", "http", "requests", "shutil"}
FORBIDDEN_STRINGS = {
    "test_verdict.publish",
    "POST",
    "PATCH",
    "PUT",
    "DELETE",
    "push",
    "--force",
    "--method",
    "-X",
    "gh",
    "git",
}


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def parity_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Source/moved SHA-256 and intentional_diff consistency of every moved file."""
    problems: list[str] = []
    if set(m["moved_scripts"]) | set(m["test_files"]) != set(m["source_parity"]):
        problems.append("moved_scripts + test_files != source_parity keys")
    for rel, info in m["source_parity"].items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != info["moved_sha256"]:
            problems.append(f"moved sha mismatch: {rel}")
        if info["verbatim"] and info["source_sha256"] != info["moved_sha256"]:
            problems.append(f"verbatim but sha differs: {rel}")
        if not info["verbatim"] and not info.get("intentional_diff"):
            problems.append(f"non-verbatim without intentional_diff: {rel}")
    if not m["source_parity"].get(MATERIALIZER, {}).get("verbatim"):
        problems.append("the materializer must be a verbatim move")
    return problems


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def deferred_problems(m: dict[str, Any]) -> list[str]:
    """C3 defers nothing: every source test runs, so any deferred record is a defect."""
    problems: list[str] = []
    if m["deferred_tests"] or m["deferred_source_tests"]:
        problems.append("C3 must have 0 deferred tests")
    t = m["totals"]
    if t["source_collected"] != sum(m["expected_collect"].values()):
        problems.append("totals.source_collected != sum(expected_collect)")
    if t["deferred"] != len(m["deferred_tests"]):
        problems.append("totals.deferred != len(deferred_tests)")
    if t["ran"] != t["source_collected"] - t["deferred"] or t["ran"] != sum(
        m["expected_ran"].values()
    ):
        problems.append("totals.ran inconsistent")
    if t["skipped"] or t["xfailed"]:
        problems.append("skipped / xfailed must be 0")
    return problems


def skip_marker_problems(m: dict[str, Any], root: Path) -> list[str]:
    """skip/xfail is forbidden everywhere in the moved tests (AST, so docstrings are ignored)."""
    problems: list[str] = []
    for rel in m["test_files"]:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and SKIP_NAMES.match(_dotted(node)):
                problems.append(f"{rel}:{node.lineno}: {_dotted(node)}")
    return problems


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def no_publish_problems(source: str) -> list[str]:
    """The materializer reads through the reused executor and writes only local files."""
    problems: list[str] = []
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    problems.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
                problems.append(f"forbidden import: {node.module}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs and node.value in FORBIDDEN_STRINGS:
                problems.append(f"forbidden string: {node.value!r} at line {node.lineno}")
        elif isinstance(node, ast.Call) and _dotted(node.func) in {
            "os.system",
            "os.popen",
            "os.execv",
            "os.execvp",
        }:
            problems.append(f"forbidden call: {_dotted(node.func)}")
    return problems


def shared_executor_problems(materializer: str, executor: str) -> list[str]:
    """The Child B functions (4 validators/readbacks + gh bin/env helpers) are looked up on the executor, never redefined here."""
    problems: list[str] = []
    mtree = ast.parse(materializer)
    defined_here = {n.name for n in ast.walk(mtree) if isinstance(n, ast.FunctionDef)}
    defined_there = {
        n.name for n in ast.walk(ast.parse(executor)) if isinstance(n, ast.FunctionDef)
    }
    docs = _docstring_ids(mtree)
    looked_up = {
        n.value
        for n in ast.walk(mtree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    }
    for name in SHARED_FUNCTIONS:
        if name in defined_here:
            problems.append(f"redefined shared function: {name}")
        if name not in defined_there:
            problems.append(f"executor lacks {name}")
        if name not in looked_up:
            problems.append(f"materializer does not look up {name}")
    return problems


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    for rel in m["required_files"] + m["test_files"]:
        assert (ROOT / rel).is_file(), rel
    assert parity_problems(m, ROOT) == []


def test_shared_executor_is_reused_not_copied() -> None:
    import importlib.util

    source = (ROOT / MATERIALIZER).read_text(encoding="utf-8")
    executor = (ROOT / EXECUTOR).read_text(encoding="utf-8")
    assert shared_executor_problems(source, executor) == []
    spec = importlib.util.spec_from_file_location(
        "materializer_for_c3_closure", ROOT / MATERIALIZER
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["materializer_for_c3_closure"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("materializer_for_c3_closure", None)
    assert mod._CONTROLLED_EXEC_PATH == ROOT / EXECUTOR
    loaded, err = mod._load_controlled_exec_module()
    assert err is None and loaded is not None
    for name in SHARED_FUNCTIONS:
        assert callable(getattr(loaded, name)), name
    assert (ROOT / IRL / "scripts" / "adjudicate_vc_result.py").is_file()


def test_no_publish_or_write_operation_in_materializer() -> None:
    assert no_publish_problems((ROOT / MATERIALIZER).read_text(encoding="utf-8")) == []


def test_manifest_deferred_is_empty_and_consistent() -> None:
    assert deferred_problems(_manifest()) == []


def test_no_skip_or_xfail_in_moved_tests() -> None:
    assert skip_marker_problems(_manifest(), ROOT) == []


def test_no_functional_legacy_slug() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", IRL],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def _collect_ids(files: list[str]) -> list[str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return [ln for ln in r.stdout.splitlines() if "::" in ln]


def test_manifest_counts() -> None:
    m = _manifest()
    actual: dict[str, int] = {}
    for line in _collect_ids(m["test_files"]):
        key = line.split("::")[0]
        actual[key] = actual.get(key, 0) + 1
    assert actual == m["expected_collect"]
    assert sum(m["expected_collect"].values()) == m["totals"]["source_collected"]
    assert m["expected_collect"] == m["expected_ran"]


def ci_lane_problems(ci_text: str) -> list[str]:
    import yaml

    jobs = (yaml.safe_load(ci_text) or {}).get("jobs", {})
    job = jobs.get(CI_JOB)
    if not isinstance(job, dict):
        return [f"job {CI_JOB} missing"]
    runs = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    required = (
        "run_stage1b1_tests.py docs/dev/i4-4c3-test-verdict-artifact-manifest.json",
        '(skipped|xfailed|xpassed|deselected)" runner.out',
        "0 deferred with reasons, 33 ran, 0 skipped",
        "tests/stage_i4",
    )
    return [f"CI step missing: {frag}" for frag in required if frag not in runs]


def test_ci_lane_is_wired_and_the_runner_passes() -> None:
    assert ci_lane_problems(CI.read_text(encoding="utf-8")) == []
    r = subprocess.run(
        [sys.executable, "-I", *RUNNER_ARGS], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 deferred with reasons, 33 ran, 0 skipped" in r.stdout


def test_fixture_only_evidence_boundary() -> None:
    b = _manifest()["evidence_boundary"]
    for key in (
        "live_e2e",
        "final_gate_wired",
        "loop_handoff_result_v1_wired",
        "test_verdict_publish_enabled",
        "real_github_readback",
        "merge_tree_oracle_real_evidence",
        "github_live_operations",
    ):
        assert b[key] is False, key
