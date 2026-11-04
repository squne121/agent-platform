"""I-4-4C2: intake capsule + triage + integration tests closure (#83).

Fixture / fake-gh evidence only. Nothing here calls live GitHub, enables
`test_verdict.publish` or touches Final Gate / `LOOP_HANDOFF_RESULT_V1`.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-4c2-intake-capsule-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
IRL = ".claude/skills/impl-review-loop"
CAPSULE = f"{IRL}/scripts/build_intake_capsule.py"
TRIAGE = f"{IRL}/scripts/triage_contract_blockers.py"
VERIFIER = f"{IRL}/scripts/verify_implementation_landed_intake_runtime.py"
CI_JOB = "i4-4c2-intake-capsule"
RUNNER_ARGS = ["scripts/ci/run_stage1b1_tests.py", "docs/dev/i4-4c2-intake-capsule-manifest.json"]
DEFERRED_OWNER = "I-4-4D"
SKIP_NAMES = re.compile(r"^pytest\.(mark\.(skip|skipif|xfail)|skip|xfail|importorskip)$")
DEFERRED_KINDS = {"unmigrated_document", "live_github_canary"}
GH_READ_SUBCOMMANDS = {"view", "list"}
GIT_READ_ARGV = (
    ["rev-parse", "HEAD"],
    ["branch", "--show-current"],
    ["status", "--short", "--porcelain=v1"],
)


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
    return problems


def deferred_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Deferred ownership and state-dependence of the deselected tests."""
    problems: list[str] = []
    deferred = m["deferred_tests"]
    if [e["id"] for e in deferred] != [e["id"] for e in m["deferred_source_tests"]]:
        problems.append("deferred_tests and deferred_source_tests differ")
    for e in deferred:
        for key in ("id", "file", "function", "reason", "owner_stage", "rerun_condition", "kind"):
            if not e.get(key):
                problems.append(f"incomplete deferred entry {e.get('id')}: {key}")
        if e.get("owner_stage") != DEFERRED_OWNER:
            problems.append(f"unexpected owner for {e.get('id')}")
        if e.get("kind") not in DEFERRED_KINDS:
            problems.append(f"unknown deferred kind for {e.get('id')}")
        if not isinstance(e.get("parametrized_cases"), int) or e["parametrized_cases"] < 1:
            problems.append(f"parametrized_cases missing for {e.get('id')}")
        if e.get("id") != f"{e.get('file')}::{e.get('function')}":
            problems.append(f"id is not file::function: {e.get('id')}")
        if e.get("kind") == "unmigrated_document":
            if not e.get("requires_missing_files"):
                problems.append(f"document entry without requires_missing_files: {e['id']}")
            for rel in e.get("requires_missing_files", []):
                if (root / rel).exists():
                    problems.append(f"stale deferred entry: {rel} now exists, restore {e['id']}")
        elif e.get("requires_missing_files"):
            problems.append(f"live canary must not claim a missing document: {e['id']}")
    if not any(e.get("kind") == "live_github_canary" for e in deferred):
        problems.append("the live GitHub canary must be an explicit deferred entry")
    t = m["totals"]
    if t["source_collected"] != sum(m["expected_collect"].values()):
        problems.append("totals.source_collected != sum(expected_collect)")
    if t["deferred"] != len(deferred) or t["ran"] != t["source_collected"] - t["deferred"]:
        problems.append("totals.deferred / ran inconsistent")
    if t["ran"] != sum(m["expected_ran"].values()):
        problems.append("totals.ran != sum(expected_ran)")
    if t["skipped"] or t["xfailed"]:
        problems.append("skipped / xfailed must be 0")
    return problems


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def skip_marker_problems(m: dict[str, Any], root: Path) -> list[str]:
    """skip/xfail (AST, so docstrings are ignored) may only occur inside an explicitly deferred function."""
    allowed = {(e["file"], e["function"]) for e in m["deferred_tests"]}
    problems: list[str] = []
    for rel in m["test_files"]:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        covered: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and (rel, node.name) in allowed:
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                covered.update(range(start, (node.end_lineno or node.lineno) + 1))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and SKIP_NAMES.match(_dotted(node))
                and node.lineno not in covered
            ):
                problems.append(f"{rel}:{node.lineno}: {_dotted(node)} outside a deferred function")
    return problems


def argv_problems(source: str) -> list[str]:
    """Every literal gh/git argv must be a read: gh view/list/GET api, git rev-parse/branch/status."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        words = [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if not words or words[0] not in {"gh", "git"}:
            continue
        if words[0] == "git":
            if words[1:] not in GIT_READ_ARGV:
                problems.append(f"non-read git argv: {words}")
        elif words[1:2] in (["pr"], ["issue"]):
            if words[2:3] and words[2] not in GH_READ_SUBCOMMANDS:
                problems.append(f"non-read gh {words[1]} subcommand: {words}")
        elif words[1:2] == ["api"]:
            write_flags = ("--method", "-X", "-f", "-F", "--field", "--raw-field", "--input")
            if any(w.startswith(write_flags) for w in words):
                problems.append(f"gh api write flag: {words}")
        else:
            problems.append(f"unexpected gh argv: {words}")
    return problems


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    for rel in m["required_files"] + m["test_files"]:
        assert (ROOT / rel).is_file(), rel
    assert parity_problems(m, ROOT) == []


def test_triage_is_a_closed_direct_dependency() -> None:
    """build_intake_capsule loads triage by sibling path; every *_PATH it loads must exist."""
    spec = importlib.util.spec_from_file_location("capsule_for_closure", ROOT / CAPSULE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["capsule_for_closure"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("capsule_for_closure", None)
    paths = {k: v for k, v in vars(mod).items() if k.endswith("_PATH") and isinstance(v, Path)}
    assert "_TRIAGE_PATH" in paths and paths["_TRIAGE_PATH"] == (ROOT / TRIAGE)
    for name, path in paths.items():
        assert path.is_file(), f"{name} -> {path}"
    assert mod._DEFAULT_REPO == "squne121/agent-platform"


def test_manifest_deferred_ownership_and_state() -> None:
    assert deferred_problems(_manifest(), ROOT) == []


def test_no_skip_or_xfail_outside_deferred_functions() -> None:
    assert skip_marker_problems(_manifest(), ROOT) == []


def test_scripts_only_run_read_commands() -> None:
    for rel in (CAPSULE, TRIAGE, VERIFIER):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert argv_problems(source) == [], rel
    assert "def build_intake_capsule" in (ROOT / CAPSULE).read_text(encoding="utf-8")


def test_no_functional_legacy_slug() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", IRL],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def _collect(files: list[str]) -> dict[str, int]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    actual: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if "::" in line:
            key = line.split("::")[0]
            actual[key] = actual.get(key, 0) + 1
    return actual


def test_manifest_counts() -> None:
    m = _manifest()
    assert _collect(m["test_files"]) == m["expected_collect"]
    assert sum(m["expected_collect"].values()) == m["totals"]["source_collected"]
    # every deferred id is a really collected test
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *m["test_files"]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    ids = {ln for ln in r.stdout.splitlines() if "::" in ln}
    assert {e["id"] for e in m["deferred_tests"]} <= ids


def test_ci_lane_is_wired_and_the_runner_passes() -> None:
    ci = CI.read_text(encoding="utf-8")
    assert f"  {CI_JOB}:" in ci
    job = ci.split(f"  {CI_JOB}:", 1)[1]
    assert "docs/dev/i4-4c2-intake-capsule-manifest.json" in job
    assert "run_stage1b1_tests.py" in job
    r = subprocess.run(
        [sys.executable, "-I", *RUNNER_ARGS], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "7 deferred with reasons, 112 ran, 0 skipped" in r.stdout


def test_fixture_only_evidence_boundary() -> None:
    b = _manifest()["evidence_boundary"]
    for key in (
        "live_e2e",
        "final_gate_wired",
        "loop_handoff_result_v1_wired",
        "test_verdict_publish_enabled",
        "merge_tree_oracle_real_evidence",
        "github_live_operations",
    ):
        assert b[key] is False, key
