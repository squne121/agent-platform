"""I-4-4A: Step 5 routing core (`route_loop_verdict_v2`) closure (#75).

Fixture evidence only: pure routing plus a temporary git repository.  Nothing here touches the
network, `gh`, or any live GitHub state, and none of it is a Final Gate / live E2E result.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-4a-router-core-manifest.json"
IRL = ".claude/skills/impl-review-loop"
ROUTER = f"{IRL}/scripts/route_loop_verdict_v2.py"
POLICY = "scripts/agent-ops/main_drift_policy.py"
ROUTER_TESTS = [
    f"{IRL}/tests/test_main_drift_routing.py",
    f"{IRL}/tests/test_already_satisfied_routing.py",
    f"{IRL}/tests/test_v2_routing_boundary_regression.py",
    f"{IRL}/tests/fixtures/step5_routing_consumer/test_route_loop_verdict_v2.py",
    f"{IRL}/tests/fixtures/step5_routing_consumer/test_route_loop_verdict_v2_merge_state_status_only.py",
]
POLICY_NAMES = {
    "MainDriftDecision",
    "classify_main_drift",
    "DEFAULT_MAX_DRIFT_REBIND_ATTEMPTS",
    "_MAIN_DRIFT_SHA",
}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _defs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in POLICY_NAMES:
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in POLICY_NAMES:
            out[node.targets[0].id] = ast.dump(node)
    return out


def test_isolated_import() -> None:
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('m',{ROUTER!r});"
        "m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m)"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    assert {ROUTER, POLICY, *ROUTER_TESTS} <= set(m["required_files"]) | set(m["test_files"])
    for rel, info in m["source_parity"].items():
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert digest == info["moved_sha256"], rel
        if info["verbatim"]:
            assert info["source_sha256"] == info["moved_sha256"], rel
        else:
            assert info.get("intentional_diff"), rel
    for rel in m["required_files"] + m["required_fixtures"] + m["test_files"]:
        assert (ROOT / rel).is_file(), rel


def test_main_drift_policy_is_reused_not_duplicated() -> None:
    m = _manifest()["main_drift_policy_reuse"]
    pinned = _defs(ROOT / POLICY)
    assert set(pinned) == POLICY_NAMES
    # the reused policy is the exact AST of the definitions embedded in the source router
    digest = hashlib.sha256(json.dumps(pinned, sort_keys=True).encode()).hexdigest()
    assert digest == m["source_router_ast_sha256"]
    assert _defs(ROOT / ROUTER) == {}, "router must import, not re-define, the drift policy"
    sys.path.insert(0, str(ROOT / IRL / "scripts"))
    try:
        import route_loop_verdict_v2 as router  # puts scripts/agent-ops on sys.path first
        import main_drift_policy
    finally:
        sys.path.pop(0)
    assert router.classify_main_drift is main_drift_policy.classify_main_drift
    assert router.MainDriftDecision is main_drift_policy.MainDriftDecision


def test_no_functional_legacy_slug() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", IRL],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_import_closure_records_unmigrated_dependencies() -> None:
    closure = json.loads((ROOT / "docs/dev/stage1a-core-manifest.json").read_text("utf-8"))
    closure = closure["import_closure"]
    imports = {e["module"]: e for e in closure["deferred_imports"]}
    refs = {e["script"]: e for e in closure["deferred_script_refs"]}
    assert "I-4-4C" in imports["pr_head_replay_publish_exec"]["owner_stage"]
    assert "I-4-4C" in refs["implementation_landed_evidence.py"]["owner_stage"]
    for rel in (
        "scripts/agent-ops/pr_head_replay_publish_exec.py",
        f"{IRL}/scripts/implementation_landed_evidence.py",
    ):
        assert not (ROOT / rel).exists(), f"{rel} is declared unmigrated"


def test_deferred_source_tests_are_recorded_not_silenced() -> None:
    m = _manifest()
    assert m["deferred_tests"] == [], "nothing is deselected/skipped at run time"
    assert m["deferred_source_tests"], "unmigrated source tests must be listed"
    for entry in m["deferred_source_tests"]:
        assert all(entry.get(k) for k in ("id", "reason", "owner_stage", "rerun_condition")), entry
        name = entry["id"].split("::")[-1]
        for rel in ROUTER_TESTS:  # absent from the migrated files: removed, never skipped
            assert name not in (ROOT / rel).read_text(encoding="utf-8"), (rel, name)
    for rel in ROUTER_TESTS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|pytest\.xfail\(", text), (
            rel
        )


def test_manifest_counts() -> None:
    m = _manifest()
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *m["test_files"]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    actual: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if "::" in line:
            actual[line.split("::")[0]] = actual.get(line.split("::")[0], 0) + 1
    assert actual == m["expected_collect"]
    assert m["expected_collect"] == m["expected_ran"]
    assert sum(m["expected_ran"].values()) == m["totals"]["ran"]


def test_fixture_only_evidence_boundary() -> None:
    m = _manifest()
    assert m["evidence_boundary"]["live_e2e"] is False
    assert m["evidence_boundary"]["final_gate_wired"] is False
    assert m["evidence_boundary"]["loop_handoff_result_v1_wired"] is False


@pytest.mark.parametrize("name", sorted(POLICY_NAMES - {"_MAIN_DRIFT_SHA"}))
def test_router_exports_policy_names(name: str) -> None:
    sys.path.insert(0, str(ROOT / IRL / "scripts"))
    try:
        import route_loop_verdict_v2 as router
    finally:
        sys.path.pop(0)
    assert hasattr(router, name)
