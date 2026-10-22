"""
.claude/skills/issue-refinement-loop/scripts/tests/test_workflow_start_entry_canonical_executor.py

Real end-to-end subprocess test (Issue #2311 fix_delta / PR #2320 review):
proves that the canonical `preflight.run` executor
(`scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run`)
actually carries an invocation-scoped `LOOP_SPARK_MODE` /
`LOOP_SPARK_FALLBACK` / `LOOP_PLANNED_OPERATIONS_JSON` capability request
through `_sanitize_env()` to the REAL `workflow_start_entry.py`, which in
turn calls the REAL `root_entry_router.capability_preflight_result()`,
which in turn invokes the REAL
`scripts/agent-ops/workflow_capability_preflight.py` producer as an
actual child subprocess -- and that an `unsupported_operation` /
`required`+`forbidden`-Spark capability request is blocked BEFORE
`run_refinement_preflight.py` (the inner preflight) is ever started.

This is deliberately NOT a fake-transport test: `wse.run()` is never called
directly and `capability_preflight_result_fn` is never monkeypatched. Every
process boundary this Issue's production `preflight.run` first hop
actually crosses is exercised for real:

    skill_runtime_exec.py (real)
      -> _sanitize_env() (real, PR #2320 review P0-1 fix under test)
      -> workflow_start_entry.py (real)
        -> root_entry_router.capability_preflight_result() (real)
          -> workflow_capability_preflight.py (real, subprocess)

Only `run_refinement_preflight.py` (the inner preflight this Issue's gate
exists to protect) and `scripts/agent-ops/worktree_catalog.py` (an
unrelated worktree-listing helper) are fixture stubs -- the former to
detect (via a marker file) whether it was ever started at all, matching
this repo's existing `_install_skill_runtime_exec_fixture` pattern in
`scripts/agent-guards/tests/test_skill_runtime_preflight_no_worktree.py`;
the latter because it is unrelated to capability-preflight gating and
would otherwise require a live GitHub API surface this test does not need.

No live GitHub API call and no live ChatGPT/ Spark proxy auth is required:
`workflow_capability_preflight.py`'s own `gh auth status` /
`gh repo view` / `sh preflight.sh --env-only` calls either fail or (for the
missing `preflight.sh` in this fixture) raise `OSError`, which the
producer's own `_run_env_only_preflight()` already catches and normalizes
to `{}` -- exactly as it does for any real environment lacking a
`claude-code-proxy` binary/auth. This test's assertions do not depend on
which of `uv`/`spark`/`github` ends up being the actual blocking reason in
this sandbox; they instead assert on the SPECIFIC `unsupported_operation`
and (when the sandbox's Spark proxy is absent, which it always is here)
`spark:unavailable` reason strings that only appear if the declared
capability request genuinely reached the producer -- proving the P0-1 env
carry-through fix, not just "some checks failed for some other reason".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


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

REPO_ROOT = Path(__file__).resolve().parents[5]


def _parse_compact_stdout(stdout: str) -> dict:
    """Minimal reader for the compact `STATUS:`/`NEXT_ACTION:`/`BLOCKERS:`
    stdout line grammar (Issue #2323) -- the SAME grammar
    `run_refinement_preflight.py::_build_compact_stdout()` renders. Only
    extracts the handful of fields this test file's real-subprocess
    assertions need; not a general-purpose grammar parser."""
    status = None
    next_action = None
    blockers: list[str] = []
    lines = stdout.splitlines()
    in_blockers = False
    for line in lines:
        if line.startswith("STATUS: "):
            status = line[len("STATUS: "):]
            in_blockers = False
        elif line.startswith("NEXT_ACTION: "):
            next_action = line[len("NEXT_ACTION: "):]
            in_blockers = False
        elif line == "BLOCKERS:":
            in_blockers = True
        elif in_blockers and line.startswith("  - "):
            blockers.append(line[len("  - "):])
        elif in_blockers and not line.startswith("  "):
            in_blockers = False
    return {"status": status, "next_action": next_action, "blockers": blockers}


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


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


def _write_from_source(repo_root: Path, rel: str) -> None:
    src = REPO_ROOT / rel
    dest = repo_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_control_plane_origin(repo_root: Path, origin: Path) -> None:
    """A real, local, deterministic ``file://`` bare remote (Issue #2199)
    that Issue #2199's dedicated-worktree lifecycle (#2196/#2197/#2198) can
    bind against with no real GitHub network access
    (``network_required: false``) -- the SAME bare + symbolic-HEAD pattern
    ``tests/agent_ops/test_control_plane_worktree_bootstrap.py``'s own
    ``_init_remote_fixture()`` already uses for #2197. Pushes ``repo_root``'s
    current ``HEAD`` (the already-committed fixture, see
    ``_install_real_capability_preflight_fixture`` below) as this bare
    remote's ``main``."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True, env=env)
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


def _install_real_capability_preflight_fixture(repo_root: Path, *, trusted_gh_bin: Path | None = None) -> None:
    """Install the REAL production first-hop chain
    (`skill_runtime_exec.py` -> `workflow_start_entry.py` ->
    `root_entry_router.capability_preflight_result()` ->
    `workflow_capability_preflight.py`), copied verbatim from this
    checkout, into a fresh temp repo. Only `run_refinement_preflight.py` is
    a fixture stub (see module docstring); Issue #2199's dedicated-worktree
    primitives (`worktree_bootstrap_exec.py`,
    `worktree_bootstrap_command_policy.py`, real `worktree_catalog.py`) are
    also copied verbatim -- bare `preflight.run` is now one of the 4
    production preflight profiles `main()` dispatches under a dedicated
    worktree bound to a real (local, deterministic) remote's
    `accepted_oid` (see `_init_control_plane_origin` above)."""
    for rel in (
        "scripts/agent-guards/repo_identity.py",
        "config/repository_identity.v1.json",
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        "scripts/agent-guards/trusted_runtime_capabilities.py",
        "scripts/agent-guards/worktree_bootstrap_command_policy.py",
        "scripts/agent-ops/worktree_bootstrap_exec.py",
        "scripts/agent-ops/worktree_catalog.py",
        "scripts/agent-ops/workflow_capability_preflight.py",
        ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
        ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
        ".claude/skills/issue-refinement-loop/scripts/root_entry_router.py",
    ):
        _write_from_source(repo_root, rel)

    if trusted_gh_bin is not None:
        # The real executor intentionally discards caller PATH and constructs
        # a fixed trusted PATH.  Keep that production selection behavior in
        # this actual-process fixture, but prepend one fixture-owned fixed
        # directory so its controlled `gh` is selected instead of any host
        # GitHub CLI.  Ambient PATH is never used.
        executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
        source = executor_path.read_text(encoding="utf-8")
        default_system_path = '''_SYSTEM_STANDARD_PATH_DIRS: tuple[str, ...] = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
'''
        fixture_system_path = f'''_SYSTEM_STANDARD_PATH_DIRS: tuple[str, ...] = (
    {str(trusted_gh_bin)!r},
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
'''
        assert default_system_path in source
        executor_path.write_text(source.replace(default_system_path, fixture_system_path), encoding="utf-8")

    # Issue #2199: source-patch the copied `skill_runtime_exec.py`'s
    # hardcoded canonical remote constant to this fixture's own local,
    # deterministic bare remote (computed here, before that remote is
    # actually created by `_init_control_plane_origin` below, so this patch
    # can be committed together with every other fixture file) -- never the
    # real `https://github.com/...` production remote (`network_required:
    # false` / `auth_required: false` per this Issue's Runtime Verification
    # Applicability).
    control_plane_origin_path = repo_root.parent / "control-plane-origin.git"
    control_plane_remote_url = control_plane_origin_path.as_uri()
    executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    executor_source = executor_path.read_text(encoding="utf-8")
    default_remote_line = 'CONTROL_PLANE_CANONICAL_REMOTE_URL = f"https://github.com/{load_trusted_repo_slug()}.git"'
    assert default_remote_line in executor_source
    fixture_remote_line = f"CONTROL_PLANE_CANONICAL_REMOTE_URL = {control_plane_remote_url!r}"
    executor_path.write_text(executor_source.replace(default_remote_line, fixture_remote_line), encoding="utf-8")

    pin = _pinned_uv_version(REPO_ROOT)
    _write_text(
        repo_root / "pyproject.toml",
        f'''[project]
name = "capability-preflight-e2e-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import os
from pathlib import Path


def _build_compact_stdout(result: dict) -> str:
    # Issue #2323: minimal fixture stand-in for the real
    # `run_refinement_preflight.py::_build_compact_stdout()` -- imported
    # by `workflow_start_entry.py` at module load time (its blocked path
    # renders the same compact grammar this real function renders for
    # ready/degraded). Only the subset of fields this fixture's own
    # blocked-path assertions need is implemented.
    lines = [
        f"STATUS: {result['status']}",
        f"NEXT_ACTION: {result['next_action']}",
    ]
    blockers = result.get("blockers", [])
    if blockers:
        lines.append("BLOCKERS:")
        for b in blockers:
            lines.append(f"  - {b}")
    return "\\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.parse_args()
    marker_path = os.environ.get("SKILL_RUNTIME_TEST_INNER_MARKER_PATH")
    if marker_path:
        marker = Path(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("inner-preflight-ran")
    probe_log_path = os.environ.get("SKILL_RUNTIME_TEST_GH_PROBE_LOG")
    if probe_log_path:
        with Path(probe_log_path).open("a", encoding="utf-8") as probe_log:
            probe_log.write("inner-preflight-start\\n")
    print('{"schema": "refinement_preflight_result/v1", "status": "ready"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )

    # Issue #2199: the dedicated worktree's child process
    # (`workflow_start_entry.py` -> ... -> `run_refinement_preflight.py`)
    # actually runs FROM the dedicated worktree checked out at the remote's
    # `accepted_oid` -- everything the child needs must be part of a REAL
    # commit this fixture pushes to its own local bare origin below, not
    # merely present, uncommitted, in the outer working tree.
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install capability preflight fixture", cwd=repo_root)
    _init_control_plane_origin(repo_root, control_plane_origin_path)


def _run_executor(
    repo: Path,
    inner_marker_path: Path,
    extra_env: dict[str, str] | None = None,
    *,
    repo_arg: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo),
        "SKILL_RUNTIME_TEST_INNER_MARKER_PATH": str(inner_marker_path),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/agent-guards/skill_runtime_exec.py")
            if cwd is not None
            else "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1228",
            "--repo",
            repo_arg if repo_arg is not None else f"{_REPO}",
        ],
        cwd=str(cwd if cwd is not None else repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write_controlled_gh(trusted_bin: Path) -> None:
    """Install a no-network GitHub CLI stand-in for the fixed trusted PATH.

    It reads only GH_CONFIG_DIR presence plus equality with a test-owned
    expected path, then records an opaque match status and command order.  It
    never opens configuration files or observes token/HOME values; forwarded
    credential keys are rejected without expanding or recording their values.
    """
    trusted_bin.mkdir(parents=True)
    gh_path = trusted_bin / "gh"
    gh_path.write_text(
        f"""#!/bin/sh
set -eu
if [ -n "${{GH_CONFIG_DIR:-}}" ] && [ "${{GH_CONFIG_DIR}}" = "${{SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR:-}}" ]; then
    gh_config_state=expected_path
else
    gh_config_state=missing_or_wrong_path
fi
if [ "${{GH_TOKEN+x}}" = x ] || \
   [ "${{GITHUB_TOKEN+x}}" = x ] || \
   [ "${{GH_ENTERPRISE_TOKEN+x}}" = x ] || \
   [ "${{GITHUB_ENTERPRISE_TOKEN+x}}" = x ]; then
    printf '%s\n' "unexpected-credential-environment" >> "${{SKILL_RUNTIME_TEST_GH_PROBE_LOG}}"
    exit 65
fi
if [ "$#" -eq 5 ] && \
   [ "$1" = "auth" ] && \
   [ "$2" = "status" ] && \
   [ "$3" = "--active" ] && \
   [ "$4" = "--hostname" ] && \
   [ "$5" = "github.com" ]; then
    probe=gh-auth
elif [ "$#" -eq 5 ] && \
     [ "$1" = "repo" ] && \
     [ "$2" = "view" ] && \
     [ "$3" = "github.com/{_REPO}" ] && \
     [ "$4" = "--json" ] && \
     [ "$5" = "name" ]; then
    probe=gh-repo-view
elif [ "$#" -eq 6 ] && \
     [ "$1" = "api" ] && \
     [ "$2" = "--hostname" ] && \
     [ "$3" = "github.com" ] && \
     [ "$4" = "repos/{_REPO}" ] && \
     [ "$5" = "--jq" ] && \
     [ "$6" = "{{name}}" ]; then
    probe=controlled-gh-api
else
    printf '%s\n' "unexpected-gh-argv" >> "${{SKILL_RUNTIME_TEST_GH_PROBE_LOG}}"
    exit 64
fi
printf '%s\\n' "${{probe}}:gh_config=${{gh_config_state}}" >> "${{SKILL_RUNTIME_TEST_GH_PROBE_LOG}}"
[ "${{gh_config_state}}" = "expected_path" ]
""",
        encoding="utf-8",
    )
    gh_path.chmod(0o755)


def test_canonical_executor_preserves_gh_config_dir_and_blocks_unmigrated_mutation_route(
    tmp_path: Path,
) -> None:
    """An isolated-HOME process crosses the real executor -> workflow-start
    -> root router -> capability producer path.  The controlled fixed-PATH
    `gh` sees only a non-secret GH_CONFIG_DIR path-identity match before the
    inner preflight starts; no GitHub network/auth/mutation is possible."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _write_controlled_gh(trusted_gh_bin)
    _install_real_capability_preflight_fixture(repo, trusted_gh_bin=trusted_gh_bin)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "launcher-gh-config"
    gh_config_dir.mkdir()
    inner_marker = tmp_path / "inner-ran.marker"
    probe_log = tmp_path / "probe-order.log"

    result = _run_executor(
        repo,
        inner_marker,
        extra_env={
            "HOME": str(isolated_home),
            "GH_CONFIG_DIR": str(gh_config_dir),
            "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
            "SKILL_RUNTIME_TEST_GH_PROBE_LOG": str(probe_log),
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            "GH_ENTERPRISE_TOKEN": "",
            "GITHUB_ENTERPRISE_TOKEN": "",
            "LOOP_PLANNED_OPERATIONS_JSON": json.dumps(
                [
                    {
                        "phase": "workflow_start",
                        "actor_role": "issue-refinement-loop",
                        "operation": "issue_comment",
                        "requires_mutation": True,
                    }
                ]
            ),
        },
    )

    # Mutation routes are not migrated (Stage 2): the read probes still run in
    # order with the preserved GH_CONFIG_DIR, but the capability is blocked and
    # the inner preflight is never started (no false-ready).
    assert result.returncode != 0, result.stdout + result.stderr
    assert "operation_route_unavailable:issue_comment" in result.stdout + result.stderr
    assert not inner_marker.exists()
    assert probe_log.read_text(encoding="utf-8").splitlines() == [
        "gh-auth:gh_config=expected_path",
        "gh-repo-view:gh_config=expected_path",
        "controlled-gh-api:gh_config=expected_path",
    ]


def test_canonical_executor_blocks_unsupported_operation_before_inner_starts(
    tmp_path: Path,
) -> None:
    """Real subprocess boundary proof for verification requirements 1, 3,
    and 4 of PR #2320 review's minimum 6 cases: the canonical executor
    carries the invocation-scoped `LOOP_PLANNED_OPERATIONS_JSON` capability
    request through to the real producer (proven by the SPECIFIC
    `unsupported_operation` reason string only the real producer, having
    actually received this exact request, would emit), and
    `run_refinement_preflight.py` is never started.

    Issue #2651 OWNER review fix_delta
    (source repository pull 2662
    P1 blocker 1): `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK` are deliberately
    NOT set here any more. This test used to set them alongside the
    unsupported-operation request to prove they never reached the real
    producer, but the canonical executor now rejects a legacy Spark
    request on its OWN input boundary BEFORE any producer dispatch at all
    (see `test_control_plane_worktree_bootstrap.
    test_given_legacy_spark_env_when_preflight_run_dispatched_then_retired_rejection_and_no_inner_dispatch`),
    so setting them here would make this test observe THAT rejection
    instead of the unsupported-operation route it exists to prove. This
    test now stays scoped to the unsupported-operation route alone."""
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    inner_marker = tmp_path / "inner-ran.marker"

    result = _run_executor(
        repo,
        inner_marker,
        extra_env={
            "LOOP_PLANNED_OPERATIONS_JSON": json.dumps(
                [
                    {
                        "phase": "workflow_start",
                        "actor_role": "issue-refinement-loop",
                        "operation": "definitely_unsupported_operation_xyz",
                        "requires_mutation": True,
                    }
                ]
            ),
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr
    # Issue #2323: the blocked path now renders the SAME compact stdout
    # line grammar (`STATUS:`/`NEXT_ACTION:`/`BLOCKERS:`) that
    # `run_refinement_preflight.py` already uses for ready/degraded --
    # instead of the previous raw `{"schema": "WORKFLOW_START_ENTRY_
    # RESULT_V1", ...}` JSON dict -- so this is parsed with the compact
    # line-grammar helper, not `json.loads`.
    parsed = _parse_compact_stdout(result.stdout)
    assert parsed["status"] == "blocked"
    # A non-trivial `BLOCKERS:` list (beyond the single fixed
    # `caller_capability_request_missing_or_malformed` marker the
    # companion negative test below asserts) proves the producer
    # subprocess was actually invoked and returned structured reasons (as
    # opposed to the zero-blockers `environment_failure` shape a
    # missing/malformed request would produce).
    assert any(
        "definitely_unsupported_operation_xyz" in blocker and "operation_route_unavailable" in blocker
        for blocker in parsed["blockers"]
    ), parsed["blockers"]
    # Issue #2651: no spark:* blocker is ever produced any more -- the env
    # var never reached the real producer (see docstring).
    assert not any(blocker.startswith("spark:") for blocker in parsed["blockers"]), parsed["blockers"]
    assert not inner_marker.exists(), "run_refinement_preflight.py must never have started"


def test_canonical_executor_fails_closed_with_no_declared_capability_request(tmp_path: Path) -> None:
    """Companion negative case: with NONE of the three `LOOP_*` env vars
    set (the real bare-`preflight.run` registry argv has no CLI flag for
    them at all), `workflow_start_entry.py` must fail closed as
    `environment_failure` BEFORE ever invoking the producer subprocess --
    unlike the `checks`-populated blocked result above, `checks` here must
    be empty, proving the producer was never reached (requirement 2 of PR
    #2320 review's minimum 6 verification cases)."""
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    inner_marker = tmp_path / "inner-ran.marker"

    result = _run_executor(repo, inner_marker, extra_env=None)

    assert result.returncode != 0, result.stdout + result.stderr
    # Issue #2323: same compact line-grammar shape as the positive case
    # above -- `BLOCKERS:` carries exactly the single typed
    # `caller_capability_request_missing_or_malformed` marker (no
    # producer-specific reasons), proving the producer was never reached.
    parsed = _parse_compact_stdout(result.stdout)
    assert parsed["status"] == "blocked"
    assert parsed["blockers"] == ["caller_capability_request_missing_or_malformed"]
    assert not inner_marker.exists()


# --- Stage 1a-2 additions: read-only ready path and executor-level negative controls ----------
# Real boundaries: skill_runtime_exec, workflow_start_entry, root_entry_router, the producer and
# trusted_runtime_capabilities. Fake boundaries: the fixed-PATH `gh` stub, the inner
# run_refinement_preflight.py (marker-writing stub), the stub worktree_catalog is NOT used here
# (the real one is copied), and a local bare origin replaces the GitHub remote.

_READ_ONLY_OPERATIONS = [
    {
        "phase": "workflow_start",
        "actor_role": "issue-refinement-loop",
        "operation": "issue_read",
        "requires_mutation": False,
    }
]


def _clean_gh_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "GH_TOKEN": "", "GITHUB_TOKEN": "", **extra}


def test_canonical_executor_read_only_declaration_reaches_inner_preflight(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _write_controlled_gh(trusted_gh_bin)
    _install_real_capability_preflight_fixture(repo, trusted_gh_bin=trusted_gh_bin)
    gh_config_dir = tmp_path / "launcher-gh-config"
    gh_config_dir.mkdir()
    inner_marker = tmp_path / "inner-ran.marker"
    probe_log = tmp_path / "probe-order.log"

    result = _run_executor(
        repo,
        inner_marker,
        extra_env=_clean_gh_env(
            tmp_path,
            GH_CONFIG_DIR=str(gh_config_dir),
            SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR=str(gh_config_dir),
            SKILL_RUNTIME_TEST_GH_PROBE_LOG=str(probe_log),
            LOOP_PLANNED_OPERATIONS_JSON=json.dumps(_READ_ONLY_OPERATIONS),
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert inner_marker.exists()
    assert probe_log.read_text(encoding="utf-8").splitlines()[-1] == "inner-preflight-start"
    assert "unverified" not in result.stdout  # permission stays a producer detail, never a grant


@pytest.mark.parametrize(
    "declaration",
    [
        "{not json",
        "[]",
        '{"phase": "x"}',
        json.dumps([{"phase": "p", "actor_role": "r", "operation": "o"}]),
        json.dumps([{"phase": "p", "actor_role": "r", "operation": "o", "requires_mutation": "no"}]),
    ],
)
def test_canonical_executor_malformed_planned_operations_fail_closed(
    tmp_path: Path, declaration: str
) -> None:
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    inner_marker = tmp_path / "inner-ran.marker"

    result = _run_executor(
        repo, inner_marker, extra_env={"LOOP_PLANNED_OPERATIONS_JSON": declaration}
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert _parse_compact_stdout(result.stdout)["status"] == "blocked"
    assert not inner_marker.exists()


def _assert_executor_rejects(
    result: subprocess.CompletedProcess[str], inner_marker: Path, reason: str
) -> None:
    """Branch / repo / worktree violations are refused by the exact-command policy gate before
    the executor's own `_validate_runtime_context` (defence in depth; that layer is pinned by
    the wrong-cwd test). Assert the gate's refusal explicitly, not just a non-zero exit."""
    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "exact command class rejected" in output, output
    assert not inner_marker.exists()


def test_canonical_executor_rejects_non_default_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    _git("checkout", "-q", "-b", "feature/x", cwd=repo)
    marker = tmp_path / "inner-ran.marker"
    result = _run_executor(
        repo, marker, extra_env={"LOOP_PLANNED_OPERATIONS_JSON": json.dumps(_READ_ONLY_OPERATIONS)}
    )
    _assert_executor_rejects(result, marker, "root_not_default_branch")


def test_canonical_executor_rejects_detached_head(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    _git("checkout", "-q", "--detach", cwd=repo)
    marker = tmp_path / "inner-ran.marker"
    result = _run_executor(
        repo, marker, extra_env={"LOOP_PLANNED_OPERATIONS_JSON": json.dumps(_READ_ONLY_OPERATIONS)}
    )
    _assert_executor_rejects(result, marker, "root_not_default_branch")


def test_canonical_executor_rejects_wrong_cwd(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    sub = repo / "sub"
    sub.mkdir()
    marker = tmp_path / "inner-ran.marker"
    result = _run_executor(
        repo,
        marker,
        extra_env={"LOOP_PLANNED_OPERATIONS_JSON": json.dumps(_READ_ONLY_OPERATIONS)},
        cwd=sub,
    )
    # Refused by the executor's own runtime-context check (not the policy gate).
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "cwd_not_canonical_main_root" in output, output
    assert not marker.exists()


def test_canonical_executor_rejects_wrong_repo_argument(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    marker = tmp_path / "inner-ran.marker"
    result = _run_executor(
        repo,
        marker,
        extra_env={"LOOP_PLANNED_OPERATIONS_JSON": json.dumps(_READ_ONLY_OPERATIONS)},
        repo_arg="example/other-repo",
    )
    _assert_executor_rejects(result, marker, "repo_binding_mismatch")


def test_canonical_executor_rejects_linked_worktree_execution(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "task/x", str(linked), cwd=repo)
    marker = tmp_path / "inner-ran.marker"
    result = _run_executor(
        linked,
        marker,
        extra_env={"LOOP_PLANNED_OPERATIONS_JSON": json.dumps(_READ_ONLY_OPERATIONS)},
    )
    _assert_executor_rejects(result, marker, "repo_binding_mismatch")


@pytest.mark.parametrize("requires_mutation", [True, False])
def test_canonical_executor_unmigrated_mutation_operation_never_reaches_inner(
    tmp_path: Path, requires_mutation: bool
) -> None:
    """Negative control: a mutation operation name is blocked whatever the caller declares for
    `requires_mutation`, so declaring it read-only cannot manufacture a ready decision."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _write_controlled_gh(trusted_gh_bin)
    _install_real_capability_preflight_fixture(repo, trusted_gh_bin=trusted_gh_bin)
    gh_config_dir = tmp_path / "launcher-gh-config"
    gh_config_dir.mkdir()
    inner_marker = tmp_path / "inner-ran.marker"
    declaration = [{**_READ_ONLY_OPERATIONS[0], "operation": "issue_edit"}]
    declaration[0]["requires_mutation"] = requires_mutation

    result = _run_executor(
        repo,
        inner_marker,
        extra_env=_clean_gh_env(
            tmp_path,
            GH_CONFIG_DIR=str(gh_config_dir),
            SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR=str(gh_config_dir),
            LOOP_PLANNED_OPERATIONS_JSON=json.dumps(declaration),
        ),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert _parse_compact_stdout(result.stdout)["status"] == "blocked"
    assert "operation_route_unavailable:issue_edit" in result.stdout + result.stderr
    assert not inner_marker.exists()
