"""Stage 1a-core: runtime wiring (silent-degrade) and direct-caller / main-drift checks."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".claude/skills/issue-refinement-loop/scripts"

# Optional imports that the scripts degrade to `None` on ImportError. In the migrated
# tree every one of them must resolve; a `None` here is a silent degrade.
WIRING = {
    "run_refinement_preflight": [
        "validate_issue_execution_decision",
        "anchor_context",
        "classify_repair_action",
        "build_structural_repair_bundle",
        "route_structural_repair_disposition",
        "detect_missing_template_sections",
        "_mrc_parse",
        "_parse_h2_sections",
        "_section_line_bounds",
        "_credentialless_read",
        "_repo_identity",
        "_main_drift_classifier_probe",
        "_EvidenceIndex",
        "_ContextBudgetReport",
    ],
    "plan_refinement_loop": [
        "classify_main_drift",
        "compute_scope_signal_delta",
        "classify_scope_delta_authority",
        "delta_extract_sections",
        "delta_iter_section_lines",
    ],
}

PROBE = """
import importlib.util, json, sys
scripts, mods = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, scripts)
out = {}
for mod, names in mods.items():
    spec = importlib.util.spec_from_file_location(mod, f"{scripts}/{mod}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod] = m
    spec.loader.exec_module(m)
    out[mod] = [n for n in names if getattr(m, n, None) is None]
print(json.dumps(out))
"""


def _unresolved(scripts: Path) -> dict[str, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-I", "-c", PROBE, str(scripts), json.dumps(WIRING)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_no_optional_import_degrades_to_none() -> None:
    assert _unresolved(SCRIPTS) == {mod: [] for mod in WIRING}


@pytest.mark.parametrize(
    ("removed", "expect_none"),
    [
        ("scripts/agent-ops/main_drift_policy.py", "classify_main_drift"),
        ("scripts/agent-ops/evidence_index.py", "_EvidenceIndex"),
        ("scripts/agent-ops/context_budget_report.py", "_ContextBudgetReport"),
    ],
)
def test_probe_detects_removed_dependency(tmp_path: Path, removed: str, expect_none: str) -> None:
    """Negative control: the wiring probe must notice a missing optional module."""
    tree = tmp_path / "t"
    for rel in (".claude/skills", "scripts/agent-ops", "scripts/agent-guards", "config"):
        shutil.copytree(ROOT / rel, tree / rel, ignore=shutil.ignore_patterns("__pycache__"))
    (tree / removed).unlink()
    found = _unresolved(tree / ".claude/skills/issue-refinement-loop/scripts")
    assert any(expect_none in names for names in found.values())


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(path.parent))


def test_run_preflight_direct_call_rejects_wrong_repo(tmp_path: Path) -> None:
    mod = _load("rrp_direct", SCRIPTS / "run_refinement_preflight.py")
    fixture = tmp_path / "f.json"
    fixture.write_text(
        (ROOT / "tests/stage1a/fixtures/readonly_positive.json")
        .read_text(encoding="utf-8")
        .replace("squne121/agent-platform", "other/x"),
        encoding="utf-8",
    )
    result, code = mod.run_preflight(100, "other/x", [], fixture_path=fixture)
    assert result["status"] == "blocked"
    assert "REPOSITORY_IDENTITY_MISMATCH" in result["blockers"]
    assert code != 0


def test_run_preflight_direct_call_fails_closed_when_identity_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load("rrp_direct_unavail", SCRIPTS / "run_refinement_preflight.py")
    broken = tmp_path / "identity.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mod._repo_identity, "DEFAULT_CONFIG_PATH", broken)
    result, code = mod.run_preflight(100, "squne121/agent-platform", [])
    assert result["status"] == "blocked"
    assert "REPOSITORY_IDENTITY_UNAVAILABLE" in result["blockers"]
    assert code != 0


SHA = {c: c * 40 for c in "abcdef"}


def _drift(**over):
    mod = _load("mdp", ROOT / "scripts/agent-ops/main_drift_policy.py")
    kw = {
        "current_base_sha": SHA["b"],
        "evidence_base_sha": SHA["a"],
        "allowed_paths_snapshot_base_sha": SHA["b"],
        "allowed_paths": ["src/"],
        "latest_main_net_diff": ["src/x.py"],
        "expected_head_sha": SHA["c"],
        "observed_head_sha": SHA["c"],
        "expected_old_sha": SHA["d"],
        "observed_old_sha": SHA["d"],
    }
    kw.update(over)
    return mod.classify_main_drift(**kw)


@pytest.mark.parametrize(
    ("over", "route", "reason"),
    [
        ({"current_base_sha": "zz"}, "hard_stop", "base_sha_fingerprint_mismatch"),
        ({"observed_head_sha": SHA["e"]}, "hard_stop", "expected_head_cas_mismatch"),
        ({"observed_old_sha": SHA["e"]}, "hard_stop", "expected_old_cas_mismatch"),
        ({"evidence_base_sha": SHA["b"]}, "fresh", None),
        (
            {"allowed_paths_snapshot_base_sha": SHA["a"]},
            "hard_stop",
            "stale_allowed_paths_snapshot",
        ),
        ({"semantic_ambiguity": True}, "hard_stop", "semantic_ambiguity"),
        ({"latest_main_net_diff": ["docs/y.md"]}, "hard_stop", "allowed_paths_conflict"),
        ({"drift_rebind_attempts": True}, "hard_stop", "main_drift_context_invalid"),
        ({"drift_rebind_attempts": -1}, "hard_stop", "main_drift_context_invalid"),
        ({"drift_rebind_attempts": 2}, "hard_stop", "concurrent_base_churn_budget_exhausted"),
        ({}, "scope_clean_reconciliation", "main_drift_scope_clean"),
        ({"behind_fast_path_eligible": True}, "behind_fast_path", "main_drift_scope_clean"),
    ],
)
def test_classify_main_drift_branches(over: dict, route: str, reason: str | None) -> None:
    decision = _drift(**over)
    assert (decision.route, decision.reason_code) == (route, reason)
    if route in {"scope_clean_reconciliation", "behind_fast_path"}:
        assert decision.evidence_epoch["drift_rebind_attempts"] == 1
        assert decision.reverify == {"snapshot": True, "ci": True, "review": True}


@pytest.mark.parametrize(
    ("script", "argv"),
    [
        (
            ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
            ["--issue", "5", "--repo", "evil/other"],
        ),
        (
            ".claude/skills/issue-refinement-loop/scripts/route_after_rewrite.py",
            [
                "--issue",
                "5",
                "--repo",
                "evil/other",
                "--state-path",
                "x",
                "--max-rewrite-attempts",
                "2",
            ],
        ),
    ],
)
def test_explicit_wrong_repo_is_rejected_before_any_gh_call(
    tmp_path: Path, script: str, argv: list[str]
) -> None:
    """A fake `gh` on PATH records any call; an untrusted --repo must exit 2 without one."""
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 1\n', encoding="utf-8")
    gh.chmod(0o755)
    env = {"PATH": f"{gh.parent}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / script), *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "repository identity mismatch" in proc.stderr, proc.stderr
    assert not log.exists(), log.read_text()
