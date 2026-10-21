"""
.claude/skills/issue-refinement-loop/scripts/tests/test_skill_runtime_exec_env_allowlist.py

Hermetic test proving that `scripts/agent-guards/skill_runtime_exec.py`'s
REAL `_sanitize_env()` function actually carries `LOOP_SPARK_MODE` /
`LOOP_SPARK_FALLBACK` / `LOOP_PLANNED_OPERATIONS_JSON` through to the child
process environment for the bare `preflight.run` command id, and strips
them for every other command id (Issue #2311 fix_delta / PR #2320 review
P0-1).

This calls the real, unmodified `_sanitize_env()` function directly (not a
reimplementation, not a fake) with a crafted `os.environ`, which is the
exact function the canonical executor's dispatch path invokes at
`env=_sanitize_env(project_root, args.command_id)` before spawning the
child `workflow_start_entry.py` process (see `_dispatch`/`main` in
`skill_runtime_exec.py`). A full end-to-end `uv run ... skill_runtime_exec.py
--command-id preflight.run ...` subprocess additionally requires canonical
main root / default branch / trusted repo binding preconditions that this
worktree's own checkout does not satisfy (this file itself is being edited
from inside a linked issue worktree, not canonical main root) -- exercising
those preconditions is out of scope for this hermetic unit boundary and is
already covered by this Issue's existing `command_registry`/
`skill_runtime_command_policy` migration-parity fixture tests. What matters
here -- the actual env-var carry-through decision -- is proven directly and
deterministically against the real function object.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as sre  # noqa: E402


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

_CAPABILITY_ENV_NAMES = (
    "LOOP_SPARK_MODE",
    "LOOP_SPARK_FALLBACK",
    "LOOP_PLANNED_OPERATIONS_JSON",
)


_GH_CONFIG_DIR_CARRIER_COMMAND_IDS = (
    "preflight.run",
    "contract_update.run.with_anchor",
    "contract_update.run.with_human_context",
    "repair_action.apply",
    "structural_repair_action.apply",
)

# Issue #2872: the three production preflight profiles whose actual child
# (`run_refinement_preflight.py`) reads Issue/comments/anchor through native
# `gh`. They carry the invocation-scoped `GH_CONFIG_DIR` exactly like bare
# `preflight.run` (#2403) so a stored GitHub CLI login configuration reaches
# the child.
_PRODUCTION_PREFLIGHT_PROFILE_COMMAND_IDS = (
    "preflight.run.with_anchor",
    "preflight.run.with_human_context",
    "preflight.run.with_agent_report",
)

_GH_CONFIG_DIR_NON_CARRIER_COMMAND_IDS = (
    "",
    "preflight.run.fixture",
    "preflight.run.fixture.with_human_context",
    "authority_transport.consume",
    "unrelated.command",
)


@pytest.mark.parametrize("command_id", _PRODUCTION_PREFLIGHT_PROFILE_COMMAND_IDS)
def test_production_preflight_profiles_carry_gh_config_dir(monkeypatch, command_id):
    """GIVEN a parent `GH_CONFIG_DIR` (a stored-login location; the value is a
    non-secret path and its contents are never read)
    WHEN `_sanitize_env()` builds the child environment for a production
    preflight profile
    THEN the SAME path reaches the child (Issue #2872 AC2)."""
    configured_path = "/non-secret/launcher-gh-config"
    monkeypatch.setenv("GH_CONFIG_DIR", configured_path)

    env = sre._sanitize_env("/fake/project/root", command_id=command_id)

    assert env["GH_CONFIG_DIR"] == configured_path


@pytest.mark.parametrize("command_id", _PRODUCTION_PREFLIGHT_PROFILE_COMMAND_IDS)
def test_production_preflight_profiles_never_synthesize_gh_config_dir(monkeypatch, command_id):
    """Unset or empty parent `GH_CONFIG_DIR` -> no value is created, inferred
    or discovered for the production preflight profiles (Issue #2872 AC2)."""
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    assert "GH_CONFIG_DIR" not in sre._sanitize_env("/fake/project/root", command_id=command_id)

    monkeypatch.setenv("GH_CONFIG_DIR", "")
    assert "GH_CONFIG_DIR" not in sre._sanitize_env("/fake/project/root", command_id=command_id)


@pytest.mark.parametrize("command_id", ("preflight.run.fixture", "preflight.run.fixture.with_human_context"))
def test_preflight_fixture_profile_does_not_carry_gh_config_dir(monkeypatch, command_id):
    """The local-only fixture lane never invokes `gh`; it must not gain the
    carrier, nor may the production-profile addition widen by prefix (Issue
    #2872 AC2)."""
    monkeypatch.setenv("GH_CONFIG_DIR", "/non-secret/launcher-gh-config")

    env = sre._sanitize_env("/fake/project/root", command_id=command_id)

    assert "GH_CONFIG_DIR" not in env


def test_gh_config_dir_carrier_set_is_exactly_the_evidence_backed_ids(monkeypatch):
    """No prefix/regex widening: probing near-miss ids shows only the exact
    finite command ids carry the path (Issue #2872 stop condition: the change
    must not extend beyond the 3 production preflight profiles)."""
    monkeypatch.setenv("GH_CONFIG_DIR", "/non-secret/launcher-gh-config")
    probed = (
        *_GH_CONFIG_DIR_CARRIER_COMMAND_IDS,
        *_PRODUCTION_PREFLIGHT_PROFILE_COMMAND_IDS,
        *_GH_CONFIG_DIR_NON_CARRIER_COMMAND_IDS,
        "preflight.run.with_anchor.extra",
        "preflight.run.with_human_context ",
        "preflight.run.with_",
        "preflight.run.",
        "preflight.run.with_agent_report.fixture",
    )
    carried = {
        command_id
        for command_id in probed
        if "GH_CONFIG_DIR" in sre._sanitize_env("/fake/project/root", command_id=command_id)
    }

    assert carried == {*_GH_CONFIG_DIR_CARRIER_COMMAND_IDS, *_PRODUCTION_PREFLIGHT_PROFILE_COMMAND_IDS}


@pytest.mark.parametrize("command_id", _GH_CONFIG_DIR_CARRIER_COMMAND_IDS)
def test_sanitize_env_carries_gh_config_dir_only_for_evidence_backed_consumers(monkeypatch, command_id):
    """Only exact GitHub CLI consumer commands retain the caller path."""
    configured_path = "/non-secret/launcher-gh-config"
    monkeypatch.setenv("GH_CONFIG_DIR", configured_path)

    env = sre._sanitize_env("/fake/project/root", command_id=command_id)

    assert env["GH_CONFIG_DIR"] == configured_path


@pytest.mark.parametrize("command_id", _GH_CONFIG_DIR_NON_CARRIER_COMMAND_IDS)
def test_sanitize_env_strips_gh_config_dir_for_non_carrier_commands(monkeypatch, command_id):
    """Profiles, mixed-mode transport, and unknown IDs never gain the carrier."""
    monkeypatch.setenv("GH_CONFIG_DIR", "/non-secret/launcher-gh-config")

    env = sre._sanitize_env("/fake/project/root", command_id=command_id)

    assert "GH_CONFIG_DIR" not in env


@pytest.mark.parametrize("command_id", _GH_CONFIG_DIR_CARRIER_COMMAND_IDS)
def test_sanitize_env_never_synthesizes_gh_config_dir_for_carrier_commands(monkeypatch, command_id):
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    assert "GH_CONFIG_DIR" not in sre._sanitize_env("/fake/project/root", command_id=command_id)

    monkeypatch.setenv("GH_CONFIG_DIR", "")
    assert "GH_CONFIG_DIR" not in sre._sanitize_env("/fake/project/root", command_id=command_id)


def test_sanitize_env_carries_planned_operations_but_not_spark_for_bare_preflight_run(monkeypatch):
    """Issue #2651: `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK` no longer pass
    through this allowlist for ANY command id, including bare
    `preflight.run` -- GPT-5.3-Codex-Spark delegation is retired. Only
    `LOOP_PLANNED_OPERATIONS_JSON` (unrelated to Spark) is still carried
    through for the bare `preflight.run` command id."""
    monkeypatch.setenv("LOOP_SPARK_MODE", "required")
    monkeypatch.setenv("LOOP_SPARK_FALLBACK", "forbidden")
    monkeypatch.setenv(
        "LOOP_PLANNED_OPERATIONS_JSON",
        '[{"phase": "p", "actor_role": "r", "operation": "issue_comment", "requires_mutation": true}]',
    )

    env = sre._sanitize_env("/fake/project/root", command_id="preflight.run")

    assert "LOOP_SPARK_MODE" not in env
    assert "LOOP_SPARK_FALLBACK" not in env
    assert env["LOOP_PLANNED_OPERATIONS_JSON"] == (
        '[{"phase": "p", "actor_role": "r", "operation": "issue_comment", "requires_mutation": true}]'
    )


def test_sanitize_env_strips_capability_request_for_sibling_commands(monkeypatch):
    """Sibling anchor-comment-driven profiles first-hop into
    `run_refinement_preflight.py` directly and never consume this env-based
    capability request -- the allowlist addition must be scoped exactly to
    the bare `preflight.run` command id and not silently widen to its
    siblings."""
    monkeypatch.setenv("LOOP_SPARK_MODE", "required")
    monkeypatch.setenv("LOOP_SPARK_FALLBACK", "forbidden")
    monkeypatch.setenv("LOOP_PLANNED_OPERATIONS_JSON", "[]")

    for command_id in (
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
        "",
    ):
        env = sre._sanitize_env("/fake/project/root", command_id=command_id)
        for env_name in _CAPABILITY_ENV_NAMES:
            assert env_name not in env, f"{env_name} leaked into command_id={command_id!r}"


def test_sanitize_env_omits_capability_env_when_unset_for_bare_preflight_run(monkeypatch):
    """When the caller never set these three env vars at all, they must
    simply be absent from the sanitized env (not present as empty strings)
    -- `workflow_start_entry.py`'s `os.environ.get(...)` fallback then
    correctly resolves to `None`, which its fail-closed `environment_failure`
    path (Issue #2311 AC5 / PR #2320 review P0-1 item 2) depends on."""
    for env_name in _CAPABILITY_ENV_NAMES:
        monkeypatch.delenv(env_name, raising=False)

    env = sre._sanitize_env("/fake/project/root", command_id="preflight.run")

    for env_name in _CAPABILITY_ENV_NAMES:
        assert env_name not in env


class _FakeDedicatedBootstrap:
    """Stand-in for `worktree_bootstrap_exec` at the single boundary the
    dedicated-worktree dispatch needs (no real worktree/network/uv work)."""

    def __init__(self, execution_root: Path) -> None:
        self._execution_root = execution_root

    def control_plane_dedicated_execution_session(self, project_root):
        from contextlib import contextmanager

        @contextmanager
        def _session():
            yield {"execution_root": str(self._execution_root)}

        return _session()

    def verify_dedicated_control_plane_identity(self, *args, **kwargs):
        return None

    def dedicated_execution_venv_dir(self, project_root):
        return str(self._execution_root / ".venv-fake")

    def ensure_dedicated_execution_environment_ready(self, **kwargs):
        return None


@pytest.mark.parametrize("command_id", _PRODUCTION_PREFLIGHT_PROFILE_COMMAND_IDS)
def test_real_dispatch_path_hands_gh_config_dir_to_child_env(monkeypatch, tmp_path, command_id):
    """Issue #2872 AC2 (dispatch path): the REAL `main()` builds the child
    environment from `_sanitize_env()`'s result -- a relative `GH_CONFIG_DIR`
    is anchored to the invocation cwd by the EXISTING dedicated-worktree
    normalization (no new discovery/copy mechanism) and reaches the monitored
    child dispatch; an unset value is never synthesized.

    Only the canonical-main-root/default-branch gates, the dedicated worktree
    bootstrap and the child process spawn are stubbed (they need a real
    primary checkout, git remote and `uv`); argv parsing, registry rendering,
    `_sanitize_env()` and the dedicated-env normalization run for real."""
    project_root = str(_REPO_ROOT)
    execution_root = tmp_path / "execution-root"
    execution_root.mkdir()
    captured: dict[str, object] = {}

    def _capture_dispatch(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_root)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("GH_CONFIG_DIR", "relative-launcher-gh-config")
    monkeypatch.delenv("LOOP_SPARK_MODE", raising=False)
    monkeypatch.delenv("LOOP_SPARK_FALLBACK", raising=False)
    monkeypatch.setattr(sre, "_normalize_and_validate_runtime_env", lambda root: [])
    monkeypatch.setattr(sre, "_validate_runtime_context", lambda root, args: tmp_path)
    monkeypatch.setattr(sre, "_load_worktree_bootstrap_exec_module", lambda: _FakeDedicatedBootstrap(execution_root))
    monkeypatch.setattr(sre, "capture_primary_checkout_invariant_snapshot", lambda root: "stable")
    monkeypatch.setattr(sre, "_is_managed_uv_project", lambda root: False)
    monkeypatch.setattr(sre, "is_exact_skill_runtime_anchor_executor_command", lambda *a, **k: True)
    monkeypatch.setattr(sre, "_dispatch_child_and_check_postconditions", _capture_dispatch)

    exit_code = sre.main(
        [
            "--command-id", command_id,
            "--issue-number", "2845",
            "--repo", f"{_REPO}",
            "--anchor-comment-url", f"https://github.com/{_REPO}/issues/2845#issuecomment-5942301496",
        ]
    )

    assert exit_code == 0
    env = captured["env"]
    assert env["GH_CONFIG_DIR"] == os.path.realpath(os.path.join(project_root, "relative-launcher-gh-config"))

    captured.clear()
    monkeypatch.delenv("GH_CONFIG_DIR")
    assert (
        sre.main(
            [
                "--command-id", command_id,
                "--issue-number", "2845",
                "--repo", f"{_REPO}",
                "--anchor-comment-url",
                f"https://github.com/{_REPO}/issues/2845#issuecomment-5942301496",
            ]
        )
        == 0
    )
    assert "GH_CONFIG_DIR" not in captured["env"]
