"""Stage 1b-1 closure / false-green controls (fixture + real-subprocess; no live GitHub).

Pins: every leaf imports under `python -I`; a missing evaluator is an import failure (never a
silent PS001/needs-fix and never a green); the evaluator is genuinely reached and its routing
consumed; layout shifts and a missing/invalid policy fail; the live-GitHub path is bound to the
repository identity before any `gh` probe.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".claude" / "skills"
CHECKER = SKILLS / "issue-contract-review" / "scripts" / "check_product_spec_contract.py"
FIXTURE = (
    SKILLS / "issue-contract-review" / "scripts" / "tests" / "fixtures" / "no_product_scope.md"
)
LEAVES = [
    ".claude/skills/create-issue/scripts/path_classification.py",
    ".claude/skills/create-issue/scripts/validate_issue_body.py",
    ".claude/skills/issue-contract-review/scripts/check_product_spec_contract.py",
    ".claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py",
    "scripts/agent-guards/extension_surface_policy_matcher.py",
    "scripts/agent-guards/changed_file_matcher.py",
]
EVALUATOR_REL = ".claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py"


def _ignore_tests(_d: str, names: list[str]) -> set[str]:
    return {n for n in names if n in {"tests", "__pycache__"}}


def _tree(tmp: Path) -> Path:
    """Minimal synthetic repo: the three skills' scripts + agent-ops/guards + config + templates."""
    for rel in (
        ".claude/skills/create-issue/scripts",
        ".claude/skills/issue-contract-review/scripts",
        ".claude/skills/impl-review-loop/scripts",
        "scripts/agent-ops",
        "scripts/agent-guards",
        "config",
        ".github/ISSUE_TEMPLATE",
        "docs/dev",
    ):
        shutil.copytree(ROOT / rel, tmp / rel, ignore=_ignore_tests)
    return tmp


def _run_checker(tree: Path, repo: str = "example-org/example-repo", *extra: str, env=None):
    script = tree / ".claude/skills/issue-contract-review/scripts/check_product_spec_contract.py"
    return subprocess.run(
        [sys.executable, "-I", str(script), "--issue-number", "1", "--repo", repo, *extra],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize("rel", LEAVES)
def test_leaf_imports_under_isolated_python(rel):
    code = (
        "import importlib.util,sys;"
        # consumers load siblings from the script's own dir (python -I omits it from sys.path)
        f"sys.path.insert(0,{str((ROOT / rel).parent)!r});"
        f"s=importlib.util.spec_from_file_location('leaf_under_test',{str(ROOT / rel)!r});"
        "m=importlib.util.module_from_spec(s);sys.modules['leaf_under_test']=m;"
        "s.loader.exec_module(m)"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr
    assert "import_failed" not in run.stdout + run.stderr


def test_checker_positive_control_passes_with_evaluator(tmp_path):
    tree = _tree(tmp_path)
    run = _run_checker(tree, "example-org/example-repo", "--body-file", str(FIXTURE))
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["decision"] == "pass"


def test_missing_evaluator_is_import_failure_not_ps001_or_green(tmp_path):
    tree = _tree(tmp_path)
    (tree / EVALUATOR_REL).unlink()
    run = _run_checker(tree, "example-org/example-repo", "--body-file", str(FIXTURE))
    assert run.returncode != 0
    assert "ModuleNotFoundError" in run.stderr and "evaluate_product_spec_gate" in run.stderr
    assert run.stdout.strip() == "", "must not emit a decision payload (no false green / PS001)"


def test_evaluator_is_reached_and_its_routing_is_consumed(tmp_path):
    tree = _tree(tmp_path)
    (tree / EVALUATOR_REL).write_text(
        "def evaluate_product_spec_payload(*a, **k):\n"
        "    return {'routing_action': 'refresh_contract_snapshot', 'reason': 'stub-reached'}\n",
        encoding="utf-8",
    )
    run = _run_checker(tree, "example-org/example-repo", "--body-file", str(FIXTURE))
    out = json.loads(run.stdout)
    assert run.returncode == 1
    assert out["decision"] == "human_judgment"
    assert out["blocked_reasons"][0]["rule_id"] == "PS001"
    assert out["blocked_reasons"][0]["excerpt"] == "stub-reached"


def test_shifted_layout_checker_fails_to_import(tmp_path):
    tree = _tree(tmp_path)
    deeper = tree / ".claude/skills/issue-contract-review/scripts/sub"
    deeper.mkdir()
    shutil.copy(CHECKER, deeper / CHECKER.name)
    run = subprocess.run(
        [
            sys.executable,
            "-I",
            str(deeper / CHECKER.name),
            "--issue-number",
            "1",
            "--repo",
            "a/b",
            "--body-file",
            str(FIXTURE),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode != 0 and "ModuleNotFoundError" in run.stderr


def test_shifted_layout_validate_issue_body_loses_templates(tmp_path):
    tree = _tree(tmp_path)
    deeper = tree / ".claude/skills/create-issue/scripts/sub"
    deeper.mkdir()
    shutil.copy(
        SKILLS / "create-issue/scripts/validate_issue_body.py", deeper / "validate_issue_body.py"
    )
    spec = importlib.util.spec_from_file_location("vib_shifted", deeper / "validate_issue_body.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vib_shifted"] = mod
    saved = list(sys.path)
    try:
        try:
            spec.loader.exec_module(mod)
        except ImportError:
            return  # shifted layout fails loudly: acceptable
        assert not (mod._REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "implementation.yml").is_file()
    finally:
        sys.path[:] = saved


def test_matcher_default_policy_path_is_repo_policy_and_exists():
    spec = importlib.util.spec_from_file_location(
        "esm_layout", ROOT / "scripts/agent-guards/extension_surface_policy_matcher.py"
    )
    assert spec and spec.loader
    saved = list(sys.path)
    try:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["esm_layout"] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    assert mod._DEFAULT_POLICY_YAML_PATH == ROOT / "docs/dev/extension-surface-runtime-policy.yaml"
    assert mod._DEFAULT_POLICY_YAML_PATH.is_file()
    assert mod.load_policy()["schema_version"] == "v2"


def test_missing_policy_yaml_raises_policy_load_error(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "esm_missing", ROOT / "scripts/agent-guards/extension_surface_policy_matcher.py"
    )
    assert spec and spec.loader
    saved = list(sys.path)
    try:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["esm_missing"] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    with pytest.raises(mod.PolicyLoadError):
        mod.load_policy(tmp_path / "absent.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(mod.PolicyLoadError):
        mod.load_policy(bad)


def _gh_stub(tmp: Path) -> tuple[Path, Path]:
    bindir = tmp / "bin"
    bindir.mkdir()
    log = tmp / "gh_calls.log"
    gh = bindir / "gh"
    gh.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\necho \'{{"title":"t","body":"","labels":[]}}\'\n',
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return bindir, log


def _env(bindir: Path) -> dict[str, str]:
    return {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin"}


def _trusted(tree: Path) -> str:
    return json.loads((tree / "config/repository_identity.v1.json").read_text())[
        "trusted_repo_slug"
    ]


def test_live_path_rejects_untrusted_repo_before_any_gh_probe(tmp_path):
    tree = _tree(tmp_path / "t")
    bindir, log = _gh_stub(tmp_path)
    run = _run_checker(tree, "example-org/other-repo", env=_env(bindir))
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"] == "repository_not_trusted"
    assert not log.exists(), "gh must not be probed for an untrusted repository"


def test_live_path_accepts_trusted_repo_and_follows_config_to_third_slug(tmp_path):
    tree = _tree(tmp_path / "t")
    bindir, log = _gh_stub(tmp_path)
    run = _run_checker(tree, _trusted(tree), env=_env(bindir))
    assert log.exists() and "issue view" in log.read_text()
    assert run.returncode in (0, 1)
    log.unlink()
    cfg = tree / "config/repository_identity.v1.json"
    cfg.write_text(
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "example-org/third"}),
        encoding="utf-8",
    )
    assert _run_checker(tree, "example-org/third", env=_env(bindir)).returncode in (0, 1)
    assert log.exists()
    log.unlink()
    assert _run_checker(tree, _trusted_orig(), env=_env(bindir)).returncode == 2
    assert not log.exists()


def _trusted_orig() -> str:
    return json.loads((ROOT / "config/repository_identity.v1.json").read_text())[
        "trusted_repo_slug"
    ]


@pytest.mark.parametrize("mode", ["missing", "invalid"])
def test_live_path_fails_closed_on_bad_identity_config(tmp_path, mode):
    tree = _tree(tmp_path / "t")
    bindir, log = _gh_stub(tmp_path)
    cfg = tree / "config/repository_identity.v1.json"
    if mode == "missing":
        cfg.unlink()
    else:
        cfg.write_text('{"schema":"x"}', encoding="utf-8")
    run = _run_checker(tree, _trusted_orig(), env=_env(bindir))
    assert run.returncode == 2
    assert not log.exists()
