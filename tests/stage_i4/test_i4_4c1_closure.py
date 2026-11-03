"""I-4-4C1: implementation_landed_evidence producer + source tests closure (#81).

Fixture evidence only: the producer is read-only and driven by fixtures / a fake `gh` runner.
Nothing here touches the network or any live GitHub state, and none of it is a Final Gate /
live E2E result.  `pr_head_replay_publish_exec.py` (merge-tree oracle, owner I-4-4C4) stays a
deferred import: this slice does not resolve its read-only / mutation boundary.
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
MANIFEST = ROOT / "docs" / "dev" / "i4-4c1-landing-evidence-manifest.json"
STAGE1A = ROOT / "docs" / "dev" / "stage1a-core-manifest.json"
IRL = ".claude/skills/impl-review-loop"
PRODUCER = f"{IRL}/scripts/implementation_landed_evidence.py"
ROUTER = f"{IRL}/scripts/route_loop_verdict_v2.py"
SKIP_MARKERS = re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|pytest\.xfail\(")
ALLOWED_DEFERRED_OWNERS = {"I-4-4C2", "I-4-4C3", "I-4-4C4"}
# Everything the router needs to load the producer from a copied layout.
LAYOUT_FILES = (
    ROUTER,
    PRODUCER,
    "scripts/agent-ops/main_drift_policy.py",
    "scripts/agent-ops/allowed_paths_policy.py",
    "scripts/agent-guards/changed_file_matcher.py",
)
UNAVAILABLE = "implementation_landed_evidence_module_unavailable"

_PROBE = (
    "import importlib.util,json,sys\n"
    "s=importlib.util.spec_from_file_location('r',sys.argv[1])\n"
    "m=importlib.util.module_from_spec(s); sys.modules['r']=m; s.loader.exec_module(m)\n"
    "ev=json.loads(sys.argv[2])\n"
    "d=m.resolve_pre_step1_landing_disposition(ev,repo=sys.argv[3],issue_number=int(sys.argv[4]))\n"
    "print(json.dumps(d))\n"
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
        if rel in m["moved_scripts"] and not info["verbatim"]:
            problems.append(f"the producer script must be moved verbatim: {rel}")
    return problems


def deferred_record_problems(stage1a: dict[str, Any], root: Path) -> list[str]:
    """The migrated producer has no deferred record; the C4 oracle import keeps its record."""
    closure = stage1a["import_closure"]
    refs = {e["script"] for e in closure["deferred_script_refs"]}
    imports = {e["module"]: e for e in closure["deferred_imports"]}
    problems: list[str] = []
    if (root / PRODUCER).exists() and "implementation_landed_evidence.py" in refs:
        problems.append("migrated producer still recorded in deferred_script_refs")
    if (root / "scripts/agent-ops/pr_head_replay_publish_exec.py").exists():
        problems.append("pr_head_replay_publish_exec.py is out of scope for C1 (owner I-4-4C4)")
    elif "pr_head_replay_publish_exec" not in imports:
        problems.append("pr_head_replay_publish_exec deferred import was dropped (C4 not done)")
    return problems


def manifest_deferred_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if m["deferred_tests"] or m["deferred_source_tests"]:
        problems.append("nothing may be deselected or removed from the moved tests")
    for e in m["deferred_dependencies"]:
        if not all(e.get(k) for k in ("path", "owner_stage", "reason", "rerun_condition")):
            problems.append(f"incomplete deferred dependency: {e.get('path')}")
        elif e["owner_stage"].split(" ")[0] not in ALLOWED_DEFERRED_OWNERS:
            problems.append(f"unknown owner: {e['path']}")
    t = m["totals"]
    if t["ran"] != sum(m["expected_ran"].values()) or t["ran"] != t["source_collected"]:
        problems.append("totals inconsistent with expected_ran / source_collected")
    if t["skipped"] or t["xfailed"] or t["deferred_run_time"]:
        problems.append("skipped / xfailed / deferred must be 0")
    return problems


def router_disposition(
    layout: Path, evidence: Any, *, repo: str = "squne121/agent-platform", issue: int = 81
) -> dict[str, Any]:
    """Run the real router from `layout` in a clean `python -I` subprocess."""
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _PROBE,
            str(layout / ROUTER),
            json.dumps(evidence),
            repo,
            str(issue),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _merged_evidence() -> dict[str, Any]:
    sha = "a" * 40
    manifest = {"ac": ["AC1", "AC2"]}
    snap = {"manifest": manifest, "content_sha256": _scope_sha(manifest)}
    return {
        "schema": "IMPLEMENTATION_LANDED_EVIDENCE_V1",
        "schema_version": 1,
        "target": {
            "repo": "squne121/agent-platform",
            "issue_number": 81,
            "body_sha256": "sha256:" + "b" * 64,
        },
        "freshness": {
            "status": "fresh",
            "observed_at": "2099-01-01T00:00:00Z",
            "max_age_seconds": 300,
        },
        "contradictory": False,
        "candidates": [
            {
                "target": {"repo": "squne121/agent-platform", "issue_number": 81},
                "pr": {
                    "number": 82,
                    "url": "https://github.com/squne121/agent-platform/pull/82",
                    "head_sha": sha,
                },
                "provenance": {"kind": "closing_relation", "verified": True},
                "lifecycle": "merged",
                "head_fresh": True,
                "current_scope_ownership": True,
                "merge_oid": sha,
                "main_ancestry": {"verified": True, "reachable": True},
                "scope_coverage": {
                    "schema": "IMPLEMENTATION_SCOPE_COVERAGE_V1",
                    "immutable_merged_snapshot": snap,
                    "live_current_scope": snap,
                    "exact_coverage": True,
                    "later_scope_expansion": False,
                    "status": "covered_exactly",
                },
            }
        ],
        "scope_coverage": None,
    }


def _scope_sha(manifest: dict[str, Any]) -> str:
    import importlib.util

    spec = importlib.util.spec_from_file_location("producer_for_sha", ROOT / PRODUCER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(
        mod.normalize_scope_coverage(manifest, manifest)["live_current_scope"]["content_sha256"]
    )


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    for rel in m["required_files"] + m["test_files"]:
        assert (ROOT / rel).is_file(), rel
    assert parity_problems(m, ROOT) == []
    assert manifest_deferred_problems(m) == []


def gh_argv_problems(source: str) -> list[str]:
    """Every literal `gh`/`git` argv in the producer must be a read (view/list/GET api)."""
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
            problems.append(f"git argv is not allowed in the read-only producer: {words}")
        elif words[1:2] in (["pr"], ["issue"]):
            if words[2:3] not in (["view"], ["list"]):
                problems.append(f"non-read gh {words[1]} subcommand: {words}")
        elif words[1:2] == ["api"]:
            write_flags = {"--method", "-X", "-f", "-F", "--field", "--raw-field", "--input"}
            if write_flags & set(words):
                problems.append(f"gh api write flag: {words}")
        else:
            problems.append(f"unexpected gh argv: {words}")
    return problems


def test_producer_is_a_pure_read_only_migration() -> None:
    source = (ROOT / PRODUCER).read_text(encoding="utf-8")
    assert gh_argv_problems(source) == []
    assert "def collect_candidate_inputs" in source  # the scan below sees the real producer


def test_no_functional_legacy_slug() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", IRL],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_stage1a_deferred_records_are_synced() -> None:
    stage1a = json.loads(STAGE1A.read_text(encoding="utf-8"))
    assert deferred_record_problems(stage1a, ROOT) == []


def test_no_skip_or_xfail_markers_in_moved_tests() -> None:
    for rel in _manifest()["test_files"]:
        assert not SKIP_MARKERS.search((ROOT / rel).read_text(encoding="utf-8")), rel


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


def test_router_loads_the_migrated_producer_positive_control() -> None:
    """The real producer runs: its own reason codes surface, never the unavailable fallback."""
    bad = router_disposition(ROOT, None)
    assert bad["disposition"] == "reconciliation_required"
    assert bad["reason_codes"] == ["evidence_not_mapping"]
    landed = router_disposition(ROOT, _merged_evidence(), issue=81)
    assert landed["disposition"] == "implementation_already_landed"
    assert UNAVAILABLE not in landed["reason_codes"]


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
