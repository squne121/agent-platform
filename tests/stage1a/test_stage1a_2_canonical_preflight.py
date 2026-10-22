"""Stage 1a-2 (#9): canonical preflight.run wiring, repository identity and failure modes.

Boundaries that are faked here are named per test: `subprocess.run` of the producer client
(unit level) or a copied tree with a different identity config (subprocess level). The real
`skill_runtime_exec -> workflow_start_entry -> root_entry_router -> producer` chain is exercised
by `test_workflow_start_entry_canonical_executor.py` (fake `gh` only).
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
SKILL_SCRIPTS = ROOT / ".claude/skills/issue-refinement-loop/scripts"
GUARDS = ROOT / "scripts/agent-guards"
OPS = ROOT / "scripts/agent-ops"
THIRD_SLUG = "example/other-repo"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def trusted_slug() -> str:
    return _load("_ri_for_stage1a_2", GUARDS / "repo_identity.py").load_trusted_repo_slug()


@pytest.fixture(scope="module")
def rer():
    return _load("_rer_for_stage1a_2", SKILL_SCRIPTS / "root_entry_router.py")


def _tree(tmp_path: Path, slug: str | None) -> Path:
    """Copy only the identity-bearing runtime into a scratch tree with its own config."""
    root = tmp_path / "tree"
    for src in (GUARDS, OPS):
        dst = root / src.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    if slug is not None:
        cfg = root / "config" / "repository_identity.v1.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": slug}),
            encoding="utf-8",
        )
    return root


def _run_py(root: Path, code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", code, *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


_POLICY_PROBE = (
    "import sys; sys.path.insert(0, 'scripts/agent-guards');"
    "import skill_runtime_command_policy as p;"
    "print(p.is_trusted_repo_slug(sys.argv[1]))"
)


# --- policy (executor binding) follows the single identity source -------------------------


def test_policy_accepts_configured_slug_and_rejects_others(tmp_path, trusted_slug):
    root = _tree(tmp_path, trusted_slug)
    assert _run_py(root, _POLICY_PROBE, trusted_slug).stdout.strip() == "True"
    assert _run_py(root, _POLICY_PROBE, "squne121/other").stdout.strip() == "False"
    assert _run_py(root, _POLICY_PROBE, THIRD_SLUG).stdout.strip() == "False"


def test_policy_follows_third_slug(tmp_path, trusted_slug):
    root = _tree(tmp_path, THIRD_SLUG)
    assert _run_py(root, _POLICY_PROBE, THIRD_SLUG).stdout.strip() == "True"
    assert _run_py(root, _POLICY_PROBE, trusted_slug).stdout.strip() == "False"


@pytest.mark.parametrize("bad", ["missing", "not-json", "bad-slug", "extra-key"])
def test_policy_fails_closed_on_unusable_identity_config(tmp_path, trusted_slug, bad):
    root = _tree(tmp_path, None if bad == "missing" else trusted_slug)
    cfg = root / "config" / "repository_identity.v1.json"
    if bad != "missing":
        cfg.write_text(
            {
                "not-json": "{",
                "bad-slug": json.dumps(
                    {"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "no-slash"}
                ),
                "extra-key": json.dumps(
                    {"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": trusted_slug, "x": 1}
                ),
            }[bad],
            encoding="utf-8",
        )
    proc = _run_py(root, _POLICY_PROBE, trusted_slug)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_control_plane_remote_url_follows_identity(tmp_path):
    root = _tree(tmp_path, THIRD_SLUG)
    code = (
        "import sys; sys.path.insert(0, 'scripts/agent-guards');"
        "import skill_runtime_exec as e; print(e.CONTROL_PLANE_CANONICAL_REMOTE_URL)"
    )
    proc = _run_py(root, code)
    assert proc.stdout.strip() == f"https://github.com/{THIRD_SLUG}.git", proc.stderr


def test_control_plane_remote_url_empty_when_identity_unusable(tmp_path):
    root = _tree(tmp_path, None)
    code = (
        "import sys; sys.path.insert(0, 'scripts/agent-guards');"
        "import skill_runtime_exec as e; print(repr(e.CONTROL_PLANE_CANONICAL_REMOTE_URL))"
    )
    assert _run_py(root, code).stdout.strip() == "''"


# --- producer CLI: identity gate and --repo default -----------------------------------------


def _producer(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(root / "scripts/agent-ops/workflow_capability_preflight.py"),
            "--profile",
            "issue-to-impl",
            "--project-root",
            str(root),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_producer_blocks_wrong_repo_before_any_probe(tmp_path, trusted_slug):
    root = _tree(tmp_path, trusted_slug)
    proc = _producer(root, "--repo", "squne121/other")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["decision"] == "blocked"
    assert result["reasons"] == ["repository_identity_mismatch"]
    assert result["checks"] == {}


def test_producer_wrong_repo_is_blocked_when_config_missing(tmp_path, trusted_slug):
    root = _tree(tmp_path, None)
    result = json.loads(_producer(root, "--repo", trusted_slug).stdout)
    assert result["decision"] == "blocked"
    assert result["reasons"] == ["repository_identity_mismatch"]


def test_producer_default_repo_unset_config_exits_2(tmp_path):
    root = _tree(tmp_path, None)
    proc = _producer(root)
    assert proc.returncode == 2
    assert json.loads(proc.stderr)["error"] == "repository_identity_unavailable"


def test_producer_gate_follows_third_slug(tmp_path, trusted_slug):
    root = _tree(tmp_path, THIRD_SLUG)
    # old slug is now foreign -> blocked before probes
    foreign = json.loads(_producer(root, "--repo", trusted_slug).stdout)
    assert foreign["reasons"] == ["repository_identity_mismatch"]
    # third slug passes the gate (probes then run; their outcome is host dependent)
    passed = json.loads(_producer(root, "--repo", THIRD_SLUG).stdout)
    assert "repository_identity_mismatch" not in passed["reasons"]
    assert passed["checks"]  # probes ran


# --- capability client: producer failure modes are never success -----------------------------


def _fake_run(monkeypatch, rer, *, returncode=0, stdout="", stderr="", exc=None):
    def _run(argv, **_kwargs):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(rer.subprocess, "run", _run)


@pytest.mark.parametrize(
    ("kwargs", "reason_prefix"),
    [
        ({"returncode": 3, "stderr": "boom"}, "producer_invocation_failed:exit_3"),
        ({"stdout": "not json"}, "producer_result_malformed:non_json_stdout"),
        ({"stdout": "[]"}, "producer_result_malformed:invalid_decision"),
        (
            {"stdout": json.dumps({"decision": "maybe"})},
            "producer_result_malformed:invalid_decision",
        ),
        ({"stdout": ""}, "producer_result_malformed:non_json_stdout"),
        (
            {"exc": subprocess.TimeoutExpired(cmd="x", timeout=1)},
            "producer_watchdog_timeout",
        ),
        ({"exc": FileNotFoundError("missing")}, "producer_invocation_failed:FileNotFoundError"),
    ],
)
def test_capability_client_failure_modes_are_blocked(
    monkeypatch, rer, trusted_slug, kwargs, reason_prefix
):
    _fake_run(monkeypatch, rer, **kwargs)
    result = rer.capability_preflight_result(repo=trusted_slug)
    assert result["decision"] == "blocked"
    assert result["reasons"][0].startswith(reason_prefix)


def test_capability_client_redacts_credentials_in_stderr(monkeypatch, rer, trusted_slug):
    monkeypatch.setenv("GH_TOKEN", "tok-should-never-surface")
    _fake_run(monkeypatch, rer, returncode=1, stderr="auth failed tok-should-never-surface")
    result = rer.capability_preflight_result(repo=trusted_slug)
    assert "tok-should-never-surface" not in json.dumps(result)


def test_capability_client_targets_agent_ops_producer(monkeypatch, rer, trusted_slug):
    seen = {}

    def _run(argv, **_kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"decision": "ready"}), stderr=""
        )

    monkeypatch.setattr(rer.subprocess, "run", _run)
    assert rer.capability_preflight_result(repo=trusted_slug)["decision"] == "ready"
    assert seen["argv"][1].endswith("scripts/agent-ops/workflow_capability_preflight.py")
    assert Path(seen["argv"][1]).is_file()


# --- workflow_start_entry: inner preflight semantics -----------------------------------------


@pytest.fixture(scope="module")
def wse():
    sys.path.insert(0, str(SKILL_SCRIPTS))
    try:
        return _load("_wse_for_stage1a_2", SKILL_SCRIPTS / "workflow_start_entry.py")
    finally:
        sys.path.remove(str(SKILL_SCRIPTS))


_PLANNED = (
    '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
    '"operation": "issue_read", "requires_mutation": false}]'
)


def _run_wse(wse, repo, decision, inner_rc=0):
    inner_calls: list[dict] = []
    producer_calls: list[dict] = []

    def _producer(**kwargs):
        producer_calls.append(kwargs)
        return {
            "decision": decision,
            "checks": {},
            "reasons": ["r"] if decision == "blocked" else [],
        }

    def _inner(**kwargs):
        inner_calls.append(kwargs)
        return inner_rc

    result, code = wse.run(
        issue_number=1,
        repo=repo,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_PLANNED,
        capability_preflight_result_fn=_producer,
        invoke_inner_preflight_fn=_inner,
    )
    return result, code, len(producer_calls), len(inner_calls)


def test_degraded_invokes_inner_exactly_once(wse, trusted_slug):
    result, code, producers, inners = _run_wse(wse, trusted_slug, "degraded")
    assert (producers, inners, code) == (1, 1, 0)
    assert result["inner_preflight_invoked"] is True


def test_inner_nonzero_exit_is_not_success(wse, trusted_slug):
    _result, code, producers, inners = _run_wse(wse, trusted_slug, "ready", inner_rc=5)
    assert (producers, inners) == (1, 1)
    assert code == 5


def test_blocked_never_reaches_inner(wse, trusted_slug):
    result, code, producers, inners = _run_wse(wse, trusted_slug, "blocked")
    assert (producers, inners) == (1, 0)
    assert result["inner_preflight_invoked"] is False
    assert code != 0


# --- registry <-> policy <-> executor wiring for preflight.run -------------------------------


def test_preflight_run_registry_target_exists_and_policy_agrees():
    sys.path.insert(0, str(GUARDS))
    try:
        policy = _load("_policy_for_stage1a_2", GUARDS / "skill_runtime_command_policy.py")
    finally:
        sys.path.remove(str(GUARDS))
    registry = _load("_registry_for_stage1a_2", SKILL_SCRIPTS / "command_registry.py")
    entry = registry.REGISTRY["preflight.run"]
    assert (
        entry["execution_class"]
        == policy.SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]["preflight.run"][
            "execution_class"
        ]
    )
    target = next(a for a in entry["argv"] if a.endswith("workflow_start_entry.py"))
    assert (ROOT / target).is_file()
    assert (ROOT / policy.SKILL_RUNTIME_EXEC_REL).is_file()


def test_no_launcher_or_proxy_dependency_in_migrated_chain():
    chain = [
        GUARDS / "skill_runtime_exec.py",
        GUARDS / "skill_runtime_command_policy.py",
        GUARDS / "trusted_runtime_capabilities.py",
        OPS / "workflow_capability_preflight.py",
        SKILL_SCRIPTS / "workflow_start_entry.py",
        SKILL_SCRIPTS / "root_entry_router.py",
    ]
    for path in chain:
        text = path.read_text(encoding="utf-8")
        assert "preflight.sh" not in text.replace("`preflight.sh`", ""), path.name
        assert "scripts/claude-gpt" not in text.replace("scripts/claude-gpt/tests", ""), path.name
    assert not (ROOT / "scripts" / "claude-gpt").exists()


# --- isolated-mode import / layout closure ---------------------------------------------------

_CHAIN = [
    "scripts/agent-guards/skill_runtime_exec.py",
    "scripts/agent-guards/skill_runtime_command_policy.py",
    "scripts/agent-guards/trusted_runtime_capabilities.py",
    "scripts/agent-ops/workflow_capability_preflight.py",
    "scripts/agent-ops/worktree_bootstrap_exec.py",
    "scripts/agent-ops/worktree_catalog.py",
    ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
    ".claude/skills/issue-refinement-loop/scripts/root_entry_router.py",
]


@pytest.mark.parametrize("rel", _CHAIN)
def test_chain_scripts_run_help_under_isolated_mode(rel):
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / rel), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "import_failed" not in proc.stdout + proc.stderr


def test_missing_identity_module_fails_the_executor_import(tmp_path, trusted_slug):
    root = _tree(tmp_path, trusted_slug)
    (root / "scripts/agent-guards/repo_identity.py").unlink()
    proc = subprocess.run(
        [sys.executable, "-I", str(root / "scripts/agent-guards/skill_runtime_exec.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0
    assert "repo_identity" in proc.stderr


def test_layout_shift_breaks_producer_path_resolution(tmp_path, trusted_slug):
    """The producer resolves scripts/agent-guards as <its dir>/../../agent-guards; one level
    deeper must fail rather than silently find another copy."""
    root = _tree(tmp_path, trusted_slug)
    shifted = root / "scripts" / "agent-ops" / "nested"
    shifted.mkdir()
    shutil.copy(root / "scripts/agent-ops/workflow_capability_preflight.py", shifted)
    proc = subprocess.run(
        [sys.executable, "-I", str(shifted / "workflow_capability_preflight.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0
