"""I-4-4C4 closure + negative controls (#87): a hollow, drifted or executor-bearing oracle must fail.

Nothing here calls GitHub or mutates a real repository.  Broken manifests and mutated sources
are built in memory / under `tmp_path`, and the checks that must reject them are the same
functions that guard the real tree.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-4c4-merge-tree-oracle-manifest.json"
STAGE1A = ROOT / "docs" / "dev" / "stage1a-core-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
ORACLE = "scripts/agent-ops/pr_head_replay_publish_exec.py"
ORACLE_TEST = "tests/stage_i4/test_i4_4c4_oracle.py"
RESTORED = "test_terminal_gate_resolves_semantic_ambiguity"
CI_JOB = "i4-4c4-merge-tree-oracle"
RUNNER_ARGS = [
    "scripts/ci/run_stage1b1_tests.py",
    "docs/dev/i4-4c4-merge-tree-oracle-manifest.json",
]
EXPECTED_FUNCTIONS = {"_run", "_merge_tree_conflicts", "compute_semantic_ambiguity"}
ALLOWED_IMPORTS = {"__future__", "subprocess", "pathlib", "collections.abc"}
# The only string constants (outside docstrings) the oracle may contain: the Git argv and the
# dash guard.  Anything else (a push / fetch / ref-update argument, a URL, ...) is a defect.
ALLOWED_STRINGS = {"git", "merge-tree", "--write-tree", "-", "utf-8"}
EXECUTOR_NAMES = {"execute", "main", "_read_pr", "_allowed_paths", "_live_ref_sha"}
FORBIDDEN_WORDS = (
    "push",
    "fetch",
    "commit",
    "worktree",
    "update-ref",
    "force",
    "ls-remote",
    "reset",
    "checkout",
    "rebase",
    "gh",
)


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _ast_digest(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()


def _functions(source: str) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}


def parity_problems(m: dict[str, Any], root: Path) -> list[str]:
    """moved SHA-256, per-function AST digests and intentional_diff of the oracle module."""
    problems: list[str] = []
    info = m["source_parity"].get(ORACLE)
    if info is None or m["moved_scripts"] != [ORACLE]:
        return ["oracle missing from source_parity / moved_scripts"]
    path = root / ORACLE
    if not path.is_file():
        return [f"missing: {ORACLE}"]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != info["moved_sha256"]:
        problems.append("moved sha mismatch")
    if info["verbatim"] or not info.get("intentional_diff"):
        problems.append(
            "the oracle is not a whole-file verbatim move and needs an intentional_diff"
        )
    funcs = _functions(raw.decode("utf-8"))
    if set(info["functions"]) != EXPECTED_FUNCTIONS:
        problems.append("manifest function set is not the three oracle functions")
    for name, rec in info["functions"].items():
        if name not in funcs:
            problems.append(f"function missing: {name}")
        elif _ast_digest(funcs[name]) != rec["ast_sha256"]:
            problems.append(f"AST digest mismatch: {name}")
        if rec["ast_identical_to_source"] != (rec["source_ast_sha256"] == rec["ast_sha256"]):
            problems.append(f"ast_identical_to_source inconsistent: {name}")
        if not rec["ast_identical_to_source"] and not rec.get("intentional_diff"):
            problems.append(f"non-identical function without intentional_diff: {name}")
    return problems


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def surface_problems(source: str) -> list[str]:
    """The oracle may only run `git merge-tree --write-tree`: no executor, entry point or mutation."""
    problems: list[str] = []
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    top = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    if top != EXPECTED_FUNCTIONS:
        problems.append(f"top-level definitions are {sorted(top)}")
    for name in sorted(top & EXECUTOR_NAMES):
        problems.append(f"executor function present: {name}")
    for node in tree.body:
        if isinstance(node, ast.If) and "__name__" in ast.dump(node.test):
            problems.append("__main__ entry point present")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        else:
            mods = []
        for mod in mods:
            if mod not in ALLOWED_IMPORTS:
                problems.append(f"unexpected import: {mod}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if node.value not in ALLOWED_STRINGS:
                problems.append(f"unexpected string constant: {node.value!r}")
            low = node.value.lower()
            problems += [
                f"forbidden word {w!r} in {node.value!r}" for w in FORBIDDEN_WORDS if low == w
            ]
    return problems


def stage1a_problems(stage1a: dict[str, Any], root: Path) -> list[str]:
    """The migrated oracle must not keep a deferred import record (and a missing one must)."""
    imports = {e["module"] for e in stage1a["import_closure"]["deferred_imports"]}
    exists = (root / ORACLE).exists()
    if exists and "pr_head_replay_publish_exec" in imports:
        return ["stale deferred import record: the oracle is migrated"]
    if not exists and "pr_head_replay_publish_exec" not in imports:
        return ["oracle absent but its deferred record was dropped"]
    return []


def manifest_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if m["deferred_tests"]:
        problems.append("the lane must not deselect any test")
    t = m["totals"]
    if t["collected"] != sum(m["expected_collect"].values()) or t["ran"] != sum(
        m["expected_ran"].values()
    ):
        problems.append("totals inconsistent with expected_collect / expected_ran")
    if t["ran"] != t["collected"] - t["deferred"] or t["skipped"] or t["xfailed"]:
        problems.append("ran != collected - deferred, or skipped / xfailed != 0")
    if set(m["test_files"]) != {ORACLE_TEST}:
        problems.append("the lane must run exactly the oracle test file")
    deferred = {d["file"]: d["top_level_test_functions"] for d in m["deferred_source_tests"]}
    if sum(deferred.values()) != 11 or len(deferred) != 2:
        problems.append("the 11 executor source tests (7 + 4) must be recorded as deferred")
    for d in m["deferred_source_tests"]:
        if not all(d.get(k) for k in ("owner_stage", "reason", "rerun_condition")):
            problems.append(f"incomplete deferred record: {d.get('file')}")
    b = m["evidence_boundary"]
    for key in (
        "live_e2e",
        "final_gate_wired",
        "loop_handoff_result_v1_wired",
        "test_verdict_publish_enabled",
        "real_github_readback",
        "github_live_operations",
        "git_ref_mutation",
    ):
        if b[key] is not False:
            problems.append(f"evidence_boundary.{key} must be false")
    return problems


def ci_lane_problems(ci_text: str) -> list[str]:
    import yaml

    job = ((yaml.safe_load(ci_text) or {}).get("jobs") or {}).get(CI_JOB)
    if not isinstance(job, dict):
        return [f"job {CI_JOB} missing"]
    runs = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    required = (
        "run_stage1b1_tests.py docs/dev/i4-4c4-merge-tree-oracle-manifest.json",
        '(skipped|xfailed|xpassed|deselected)" runner.out',
        "0 deferred with reasons, 25 ran, 0 skipped",
        "tests/stage_i4",
    )
    return [f"CI step missing: {frag}" for frag in required if frag not in runs]


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_manifest())


@pytest.fixture
def oracle_source() -> str:
    return (ROOT / ORACLE).read_text(encoding="utf-8")


# --- positive closure of the real tree ---------------------------------------------------------


def test_oracle_module_matches_manifest_parity_and_ast_digests(manifest):
    assert parity_problems(manifest, ROOT) == []
    funcs = manifest["source_parity"][ORACLE]["functions"]
    assert funcs["_run"]["ast_identical_to_source"] is True
    assert funcs["_merge_tree_conflicts"]["ast_identical_to_source"] is True
    assert funcs["compute_semantic_ambiguity"]["ast_identical_to_source"] is False


def test_oracle_only_surface_has_no_executor_or_ref_mutation(oracle_source):
    assert surface_problems(oracle_source) == []
    signature = ast.unparse(_functions(oracle_source)["compute_semantic_ambiguity"].args)
    assert signature == "base_sha: str, candidate_sha: str, *, cwd: Path, runner: Runner=_run"


def test_manifest_counts_match_pytest_collection(manifest):
    assert manifest_problems(manifest) == []
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--co",
            "-q",
            "-p",
            "no:cacheprovider",
            *manifest["test_files"],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    ids = [ln for ln in r.stdout.splitlines() if "::" in ln]
    assert {ORACLE_TEST: len(ids)} == manifest["expected_collect"]


def test_deferred_executor_tests_are_recorded_and_not_collected(manifest):
    names = {Path(d["file"]).name for d in manifest["deferred_source_tests"]}
    assert names == {"test_pr_head_replay_publish_exec.py", "test_main_drift_replay_publish.py"}
    for d in manifest["deferred_source_tests"]:
        assert not (ROOT / d["file"]).exists(), "executor tests must not be migrated by C4"
    for name in names:
        assert not list(ROOT.rglob(name)), name
    assert (
        RESTORED
        in manifest["restored_source_tests"][next(iter(manifest["restored_source_tests"]))][
            "restored_in"
        ]
    )


def test_restored_terminal_gate_test_is_ast_identical_to_the_source(manifest):
    rec = next(iter(manifest["restored_source_tests"].values()))
    tree = ast.parse((ROOT / ORACLE_TEST).read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == RESTORED)
    assert _ast_digest(fn) == rec["ast_sha256"]


def test_import_closure_has_no_oracle_deferred_import():
    stage1a = json.loads(STAGE1A.read_text(encoding="utf-8"))
    assert stage1a_problems(stage1a, ROOT) == []
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_import_closure.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_ci_lane_is_wired_and_the_runner_passes():
    assert ci_lane_problems(CI.read_text(encoding="utf-8")) == []
    r = subprocess.run(
        [sys.executable, "-I", *RUNNER_ARGS], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 deferred with reasons, 25 ran, 0 skipped" in r.stdout


def test_no_functional_legacy_slug():
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", "scripts", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


# --- negative controls ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "injection",
    [
        "\n\ndef execute(*a, **k):\n    return 1\n",
        "\n\ndef main(argv=None):\n    return 0\n",
        "\n\nif __name__ == '__main__':\n    main()\n",
        "\n\ndef _publish(root):\n    return ['git', 'push', '--force-with-lease']\n",
        "\n\ndef _ref(root):\n    return ['git', 'update-ref', 'refs/heads/x', 'HEAD']\n",
        "\n\ndef _fetch(root):\n    return ['git', 'fetch', 'origin']\n",
        "\nimport argparse\n",
        "\nimport json\n",
    ],
)
def test_surface_check_rejects_injected_executor_push_and_ref_mutation(oracle_source, injection):
    assert surface_problems(oracle_source) == []
    assert surface_problems(oracle_source + injection) != []


def test_surface_check_rejects_a_widened_merge_tree_argv(oracle_source):
    widened = oracle_source.replace('"--write-tree"', '"--write-tree", "--messages"')
    assert widened != oracle_source and surface_problems(widened) != []


def test_manifest_tamper_is_detected(manifest, tmp_path):
    def tampered(mutate) -> list[str]:
        m = copy.deepcopy(manifest)
        mutate(m)
        return parity_problems(m, ROOT) + manifest_problems(m)

    info = lambda m: m["source_parity"][ORACLE]
    assert tampered(lambda m: None) == []
    assert tampered(lambda m: info(m).update(moved_sha256="0" * 64))
    assert tampered(lambda m: info(m)["functions"]["_run"].update(ast_sha256="0" * 64))
    assert tampered(lambda m: info(m)["functions"].pop("_run"))
    assert tampered(lambda m: info(m).update(verbatim=True))
    assert tampered(lambda m: info(m).update(intentional_diff=""))
    assert tampered(
        lambda m: info(m)["functions"]["compute_semantic_ambiguity"].pop("intentional_diff")
    )
    assert tampered(lambda m: m["totals"].update(ran=m["totals"]["ran"] - 1))
    assert tampered(lambda m: m["totals"].update(skipped=1))
    assert tampered(lambda m: m["deferred_tests"].append({"id": "x"}))
    assert tampered(lambda m: m["deferred_source_tests"].pop())
    assert tampered(lambda m: m["deferred_source_tests"][0].update(rerun_condition=""))
    assert tampered(lambda m: m["evidence_boundary"].update(git_ref_mutation=True))
    assert tampered(lambda m: m["test_files"].append("tests/stage_i4/test_other.py"))
    # a missing oracle file is reported, not ignored
    assert parity_problems(manifest, tmp_path) == [f"missing: {ORACLE}"]


def test_stale_stage1a_deferred_import_is_detected(tmp_path):
    stage1a = json.loads(STAGE1A.read_text(encoding="utf-8"))
    stale = copy.deepcopy(stage1a)
    stale["import_closure"]["deferred_imports"].append(
        {"module": "pr_head_replay_publish_exec", "reason": "x", "owner_stage": "I-4-4C"}
    )
    assert stage1a_problems(stale, ROOT) != []
    # the existing C1 check (oracle-present branch) agrees
    spec = importlib.util.spec_from_file_location(
        "c1_closure_for_c4", ROOT / "tests" / "stage_i4" / "test_i4_4c1_closure.py"
    )
    assert spec is not None and spec.loader is not None
    c1 = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = c1
    try:
        spec.loader.exec_module(c1)
    finally:
        sys.modules.pop(spec.name, None)
    assert c1.deferred_record_problems(stage1a, ROOT) == []
    assert c1.deferred_record_problems(stale, ROOT) != []
    # without the oracle file, dropping the record is the defect
    assert stage1a_problems(stage1a, tmp_path) != []


def test_router_copied_layout_without_oracle_fails_closed(tmp_path, monkeypatch):
    irl = tmp_path / ".claude/skills/impl-review-loop/scripts"
    irl.mkdir(parents=True)
    shutil.copy(ROOT / ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py", irl)
    (tmp_path / "scripts/agent-ops").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/agent-ops/main_drift_policy.py", tmp_path / "scripts/agent-ops")
    spec = importlib.util.spec_from_file_location(
        "route_copy_for_c4", irl / "route_loop_verdict_v2.py"
    )
    assert spec is not None and spec.loader is not None
    copy_mod = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(
        sys,
        "path",
        [p for p in sys.path if not (Path(p or ".") / "pr_head_replay_publish_exec.py").exists()],
    )
    monkeypatch.delitem(sys.modules, "pr_head_replay_publish_exec", raising=False)
    monkeypatch.setitem(sys.modules, spec.name, copy_mod)
    spec.loader.exec_module(copy_mod)

    sha_a, sha_b = "a" * 40, "b" * 40
    live = {
        "head_sha": sha_a,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "main_drift": {
            "current_base_sha": sha_a,
            "evidence_base_sha": sha_b,
            "allowed_paths_snapshot_base_sha": sha_a,
            "allowed_paths": ["f.txt"],
            "latest_main_net_diff": ["f.txt"],
            "expected_old_sha": sha_a,
            "observed_old_sha": sha_a,
        },
    }
    verdict = {"verdict": "APPROVE", "reviewed_head_sha": sha_a, "blockers": [], "warnings": []}
    decision = copy_mod.route_loop_verdict_v2_resolve_semantic_ambiguity(
        verdict, live, cwd=tmp_path
    )
    assert decision.route == "fail_closed" and decision.fail_closed is True
    assert decision.reason_code == "semantic_ambiguity_oracle_unavailable"
    assert not (tmp_path / ORACLE).exists()
