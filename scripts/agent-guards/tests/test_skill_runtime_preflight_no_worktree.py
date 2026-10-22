from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


def _load_trusted_slug() -> str:
    """Trusted slug from the single identity source (config/repository_identity.v1.json)."""
    import importlib.util as _ilu

    for parent in Path(__file__).resolve().parents:
        module_path = parent / "scripts" / "agent-guards" / "repo_identity.py"
        if module_path.is_file():
            spec = _ilu.spec_from_file_location("_repo_identity_for_tests", module_path)
            module = _ilu.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.load_trusted_repo_slug()
    raise RuntimeError("repo_identity.py not found")


_REPO = _load_trusted_slug()


REPO_ROOT = Path(__file__).resolve().parents[3]


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


LOCAL_MAIN_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "local_main_branch_guard.sh"
WORKTREE_SCOPE_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", f"https://github.com/{_REPO}.git", cwd=repo)
    # Issue #2199: mirrors this real repo's own `.gitignore` entry for
    # `.claude/worktrees/` -- the fixed dedicated-worktree path (#2197)
    # `capture_primary_checkout_invariant_snapshot()`'s `git status` must
    # NOT see as untracked drift once `main()` creates it under this
    # fixture's own `project_root`.
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.claude/worktrees/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _detach_head(repo: Path) -> None:
    _git("checkout", "--detach", "HEAD", cwd=repo)


def _switch_to_non_default_branch(repo: Path) -> None:
    _git("switch", "-c", "topic/no-worktree-negative", cwd=repo)


def _payload(command: str, cwd: Path) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})


def _run_guard(
    script: Path,
    payload: str,
    repo: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    env.pop("LOOP_ISSUE_NUMBER", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_control_plane_origin(repo_root: Path, origin: Path) -> None:
    """A real, local, deterministic ``file://`` bare remote (Issue #2199)
    that Issue #2199's dedicated-worktree lifecycle (#2196/#2197/#2198) can
    bind against with no real GitHub network access
    (``network_required: false``) -- the SAME bare + symbolic-HEAD pattern
    ``tests/agent_ops/test_control_plane_worktree_bootstrap.py``'s own
    ``_init_remote_fixture()`` already uses for #2197. Pushes ``repo_root``'s
    current ``HEAD`` (the already-committed fixture, see
    ``_install_skill_runtime_exec_fixture`` below) as this bare remote's
    ``main``."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True, env=env
    )
    subprocess.run(
        ["git", "push", "-q", str(origin), "HEAD:refs/heads/main"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
        env=env,
    )


def _execution_root(repo_root: Path) -> Path:
    """The one fixed dedicated worktree path Issue #2199's wired `main()`
    dispatches the 4 production preflight profiles' child process under
    (mirrors `worktree_bootstrap_exec.fixed_control_plane_worktree_path()`)
    -- this fixture's own artifact-existence assertions must look here, not
    under `repo_root`, once the child's cwd is the dedicated worktree."""
    return repo_root / ".claude" / "worktrees" / "control-plane-preflight"


def _install_skill_runtime_exec_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        "scripts/agent-guards/repo_identity.py",
        "config/repository_identity.v1.json",
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        "scripts/agent-guards/worktree_bootstrap_command_policy.py",
        "scripts/agent-ops/worktree_bootstrap_exec.py",
        "scripts/agent-ops/worktree_catalog.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

    # Issue #2199: `preflight.run` is now one of the 4 production preflight
    # profiles `main()` dispatches under a dedicated worktree bound to a
    # real remote's `accepted_oid` (#2197). Source-patch the copied
    # `skill_runtime_exec.py`'s hardcoded canonical remote constant to this
    # fixture's own local, deterministic bare remote (computed here, before
    # that remote is actually created by `_init_control_plane_origin`
    # below, so this patch can be committed together with every other
    # fixture file) -- never the real `https://github.com/...` production
    # remote (`network_required: false` / `auth_required: false` per this
    # Issue's Runtime Verification Applicability). This is the same
    # source-patch technique this repo's sibling
    # `test_workflow_start_entry_canonical_executor.py` already uses for
    # its own fixed-PATH `gh` override.
    control_plane_origin_path = repo_root.parent / "control-plane-origin.git"
    control_plane_remote_url = control_plane_origin_path.as_uri()
    executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    executor_source = executor_path.read_text(encoding="utf-8")
    default_remote_line = (
        'CONTROL_PLANE_CANONICAL_REMOTE_URL = f"https://github.com/{load_trusted_repo_slug()}.git"'
    )
    assert default_remote_line in executor_source
    fixture_remote_line = f"CONTROL_PLANE_CANONICAL_REMOTE_URL = {control_plane_remote_url!r}"
    executor_path.write_text(
        executor_source.replace(default_remote_line, fixture_remote_line), encoding="utf-8"
    )

    pin = _pinned_uv_version(source_root)
    _write_text(
        repo_root / "pyproject.toml",
        f'''[project]
name = "skill-runtime-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
    )

    _write_text(
        # Issue #2311 AC1 fixture parity: bare `preflight.run` first-hops
        # into `workflow_start_entry.py` (a minimal fixture-local forwarder
        # to `run_refinement_preflight.py` below -- see that file) instead
        # of `run_refinement_preflight.py` directly.
        repo_root
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / "workflow_start_entry.py",
        """from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    _inner = Path(__file__).resolve().parent / "run_refinement_preflight.py"
    _proc = subprocess.run([sys.executable, str(_inner), *sys.argv[1:]])
    raise SystemExit(_proc.returncode)
""",
    )

    _write_text(
        repo_root
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv",
            "run",
            "python3",
            ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
            "--issue-number",
            "{issue_number}",
            "--repo",
            "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    }
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    argv = REGISTRY[command_id]["argv"]
    rendered = []
    for token in argv:
        if token == "{issue_number}":
            rendered.append(str(values["issue_number"]))
        elif token == "{repo}":
            rendered.append(str(values["repo"]))
        else:
            rendered.append(token)
    return rendered
""",
    )

    _write_text(
        repo_root
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    marker_path = os.environ.get("SKILL_RUNTIME_TEST_MARKER_PATH")
    if marker_path:
        marker = Path(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("child-ran")
    if os.environ.get("SKILL_RUNTIME_TEST_OUTSIDE_WRITE") == "ignored":
        ignored_dir = Path(".cache")
        ignored_dir.mkdir(parents=True, exist_ok=True)
        (ignored_dir / "outside.txt").write_text("persisted")
    if os.environ.get("SKILL_RUNTIME_TEST_ARTIFACT_MODE") == "outside_projection":
        print("ARTIFACT:")
        print("  staged_result: .cache/stale.txt")
        return 0
    payload = {"issue_number": args.issue_number, "repo": args.repo}
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )

    # Issue #2199: the dedicated worktree's child process
    # (`workflow_start_entry.py` -> `run_refinement_preflight.py`) actually
    # runs FROM the dedicated worktree checked out at the remote's
    # `accepted_oid` -- everything the child needs must be part of a REAL
    # commit this fixture pushes to its own local bare origin below, not
    # merely present, uncommitted, in the outer working tree.
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install skill runtime fixture", cwd=repo_root)
    _init_control_plane_origin(repo_root, control_plane_origin_path)


def _run_executor(
    repo: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1228",
            "--repo",
            f"{_REPO}",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _stale_tmp_dir(repo: Path, issue_dir_name: str = "issue-1220-refinement") -> Path:
    stale_tmp = repo / ".claude" / "worktrees" / issue_dir_name / "tmp"
    stale_tmp.mkdir(parents=True, exist_ok=True)
    return stale_tmp


def test_artifact_only_write_postcondition_artifact_only_write_postcondition(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_OUTSIDE_WRITE": "ignored"})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.cache/" in result.stderr
    assert "target_issue=1228" in result.stderr


def test_stale_worktree_preflight_does_not_write_other_issue_tmp(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    marker_path = (
        repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "child-ran.txt"
    )
    stale_tmp = _stale_tmp_dir(repo, "issue-9999-tmp")
    result = _run_executor(
        repo,
        {
            "TMPDIR": str(stale_tmp),
            "SKILL_RUNTIME_TEST_MARKER_PATH": str(marker_path),
        },
    )
    assert result.returncode == 2
    assert "reason_code=stale_worktree_runtime_state" in result.stderr
    assert "target_issue=1228" in result.stderr
    assert ".claude/worktrees/issue-9999-tmp/tmp" in result.stderr
    assert not marker_path.exists()
    assert not (stale_tmp / "outside.txt").exists()


def test_stale_worktree_runtime_state_precheck(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    marker_path = (
        repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "child-ran.txt"
    )
    stale_tmp = _stale_tmp_dir(repo)
    stale_env = {
        "TMPDIR": str(stale_tmp),
        "SKILL_RUNTIME_TEST_MARKER_PATH": str(marker_path),
    }
    result = _run_executor(repo, stale_env)
    assert result.returncode == 2
    assert "stale_worktree_runtime_state" in result.stderr
    assert ".claude/worktrees/issue-1220-refinement/tmp" in result.stderr
    assert "source_env=TMPDIR" in result.stderr
    assert not marker_path.exists()
    assert not (
        repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    ).exists()


def test_stale_worktree_artifact_projection_fail_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_ARTIFACT_MODE": "outside_projection"})
    assert result.returncode == 2
    assert "reason_code=stale_worktree_runtime_state" in result.stderr
    assert ".cache/stale.txt" in result.stderr
    assert ".cache/stale.txt" not in result.stdout
