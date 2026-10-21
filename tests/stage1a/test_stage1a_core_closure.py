"""Stage 1a-core: real read-only preflight in an isolated copy of the closure.

Each test copies the runtime closure into a temp tree and runs the real
`run_refinement_preflight.py` against a fake-transport fixture. A fake-transport
success is NOT a real GitHub success, and a read-only `warn` is not a terminal
success of the refinement loop.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "readonly_positive.json"
CLOSURE = [
    ".claude/skills/issue-refinement-loop",
    ".claude/skills/issue-contract-review",
    ".claude/skills/create-issue",
    "scripts/agent-ops",
    "scripts/agent-guards",
    "config",
    ".github/ISSUE_TEMPLATE",
    "docs/dev/github-ops.md",
]
SKILL = ".claude/skills/issue-refinement-loop"
CANONICAL = "squne121/agent-platform"
# Old repository slug, assembled at runtime so no functional literal exists in the tree.
LEGACY = "/".join(["squne121", "-".join(["loop", "protocol"])])  # noqa: FLY002
RESULT = ".claude/artifacts/issue-refinement-loop/100/refinement_preflight_result_v1.json"
OK_STATUS = {"pass", "warn"}


def _copy(dst: Path, *, skill_prefix: str = "") -> Path:
    for rel in CLOSURE:
        src = ROOT / rel
        target = dst / (skill_prefix + rel if rel.startswith(".claude") else rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        else:
            shutil.copy2(src, target)
    subprocess.run(["git", "init", "-q", str(dst)], check=True)
    return dst


def _fixture(dst: Path, repo: str) -> Path:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["repo"] = repo
    path = dst / "fixture.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run(tree: Path, repo: str, *, fixture_repo: str | None = None, skill_prefix: str = ""):
    shutil.rmtree(tree / skill_prefix / ".claude" / "artifacts", ignore_errors=True)
    script = tree / skill_prefix / SKILL / "scripts" / "run_refinement_preflight.py"
    proc = subprocess.run(
        [
            sys.executable,
            "-W",
            "ignore",
            "-I",
            str(script),
            "--issue-number",
            "100",
            "--repo",
            repo,
            "--fixture",
            str(_fixture(tree, fixture_repo or repo)),
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    result_path = tree / skill_prefix / RESULT
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        # Early blocks (identity binding) print the compact result and write no artifact.
        status = next(
            (
                ln.split(":", 1)[1].strip()
                for ln in proc.stdout.splitlines()
                if ln.startswith("STATUS:")
            ),
            None,
        )
        result = {"status": status, "stdout": proc.stdout} if status else None
    return proc, result


def _assert_positive(proc, result):
    assert result is not None, proc.stdout + proc.stderr
    assert result["status"] in OK_STATUS, result
    assert result["blockers"] == [], result
    assert result["planner_exit_code"] == 0 and result["planner_fail_closed"] is False, result


def _blocked(proc, result, needle: str):
    assert result is not None, proc.stdout + proc.stderr
    assert result["status"] not in OK_STATUS, result
    assert needle in json.dumps(result), result
    assert proc.returncode != 0, proc.stdout


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return _copy(tmp_path / "t")


def test_readonly_positive_reaches_planner(tree: Path) -> None:
    proc, result = _run(tree, CANONICAL)
    _assert_positive(proc, result)


def test_wrong_repository_rejected(tree: Path) -> None:
    _blocked(*_run(tree, "someone/else"), "REPOSITORY_IDENTITY_MISMATCH")


def test_legacy_slug_rejected(tree: Path) -> None:
    _blocked(*_run(tree, LEGACY), "REPOSITORY_IDENTITY_MISMATCH")


def test_fixture_repo_mismatch_rejected(tree: Path) -> None:
    _blocked(*_run(tree, CANONICAL, fixture_repo="someone/else"), "REPOSITORY_IDENTITY_MISMATCH")


def test_third_slug_follows_config(tree: Path) -> None:
    cfg = tree / "config" / "repository_identity.v1.json"
    cfg.write_text(
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "acme/widgets"}),
        encoding="utf-8",
    )
    _assert_positive(*_run(tree, "acme/widgets"))
    _blocked(*_run(tree, CANONICAL), "REPOSITORY_IDENTITY_MISMATCH")


def test_unset_identity_config_fails_closed(tree: Path) -> None:
    (tree / "config" / "repository_identity.v1.json").unlink()
    _blocked(*_run(tree, CANONICAL), "REPOSITORY_IDENTITY_UNAVAILABLE")


def test_missing_identity_module_fails_closed(tree: Path) -> None:
    (tree / "scripts" / "agent-guards" / "repo_identity.py").unlink()
    proc, result = _run(tree, CANONICAL)
    # Import-time crash is also fail-closed: never a positive result.
    assert proc.returncode != 0 and (result is None or result["status"] not in OK_STATUS)
    assert "repo_identity.py" in proc.stderr or "REPOSITORY_IDENTITY_UNAVAILABLE" in proc.stdout


def test_missing_runtime_dependency_is_not_positive(tree: Path) -> None:
    (tree / SKILL / "scripts" / "repair_issue_contract.py").unlink()
    proc, result = _run(tree, CANONICAL)
    assert result is None or result["status"] not in OK_STATUS, proc.stdout


def test_layout_shift_is_not_positive(tmp_path: Path) -> None:
    tree = _copy(tmp_path / "t", skill_prefix="shifted/")
    proc, result = _run(tree, CANONICAL, skill_prefix="shifted/")
    assert result is None or result["status"] not in OK_STATUS, proc.stdout


def test_baseline_dependency_present_for_scope_reframe_check() -> None:
    """`_check_scope_reframe_deltas_reflected` swallows load errors into
    `invalid_or_unavailable`; the target and its imports must therefore resolve."""
    script = ROOT / ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "; ".join(
                [
                    "import importlib.util, sys",
                    "sys.argv = ['x']",
                    f"s = importlib.util.spec_from_file_location('b', {str(script)!r})",
                    "m = importlib.util.module_from_spec(s)",
                    "s.loader.exec_module(m)",
                ]
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_import_closure_checker_flags_missing_import(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "check_import_closure", ROOT / "scripts" / "ci" / "check_import_closure.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path / "s"
    d.mkdir()
    (d / "a.py").write_text("import json\nimport b_present\nimport no_such_mod_xyz\n")
    (d / "b_present.py").write_text("X = 1\n")
    assert mod.unresolved(tmp_path, ["s"], ["s"]) == {"no_such_mod_xyz": ["s/a.py"]}
    (d / "c.py").write_text("P = 'gone_script.py'\n")
    assert mod.unresolved_script_refs(tmp_path, ["s"], ["s"]) == {"gone_script.py": ["s/c.py"]}
