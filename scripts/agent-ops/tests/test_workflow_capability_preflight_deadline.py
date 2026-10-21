"""Hermetic shared-deadline tests for Issue #2322."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
_ROOT_ROUTER_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"

for _path in (_SCRIPTS_DIR, _GUARDS_DIR, _ROOT_ROUTER_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import root_entry_router as rer
import workflow_capability_preflight as wcp


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


def _ready_uv(*_args):
    return {
        "status": wcp.trusted_uv_mod.STATUS_OK,
        "reason": "resolved",
        "resolved_path": "/fake/uv",
    }


def _completed(argv, *, stdout=""):
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_all_three_required_probes_share_decreasing_remaining_timeout(monkeypatch):
    """Issue #2651: the optional Spark env-only probe is retired -- only the
    3 required GitHub probes (`github_auth` -> `github_repo_read` ->
    `controlled_github_read`) share the deadline any more, even when
    `spark_mode="required"` is declared (the directive is judged
    deterministically retired, without spawning any process for it)."""
    clock_values = iter((90_000_000_000, 91_000_000_000, 92_000_000_000, 93_000_000_000))
    monkeypatch.setattr(wcp.time, "monotonic_ns", lambda: next(clock_values))
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", _ready_uv)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return _completed(argv)

    monkeypatch.setattr(wcp.subprocess, "run", fake_run)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations=[],
        deadline_ns=100_000_000_000,
    )

    # The overall decision is `blocked` (Spark directive retired), not
    # `ready` -- this test's own subject is the shared-deadline timeout
    # sequence, not the decision outcome (covered elsewhere).
    assert result["checks"]["spark"]["status"] == wcp.SPARK_RETIRED
    assert [round(timeout, 3) for _, timeout in calls] == [10.0, 9.0, 8.0]
    assert [call[0][0] for call in calls] == ["gh", "gh", "gh"]


def test_expired_deadline_spawns_no_new_process(monkeypatch):
    monkeypatch.setattr(wcp.time, "monotonic_ns", lambda: 100)
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", _ready_uv)
    calls = []
    monkeypatch.setattr(wcp.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
        deadline_ns=100,
    )

    assert calls == []
    assert "preflight_deadline_exhausted:github_auth" in result["reasons"]
    assert "preflight_deadline_exhausted:controlled_github_read" in result["reasons"]


def test_spark_probe_is_never_spawned_for_any_spark_mode_value(monkeypatch):
    """Issue #2651: no `spark_mode` value spawns the (retired) Spark
    env-only probe any more -- not `None` (never did), and not
    `preferred`/`required` either (used to, before retirement). Every
    non-None directive now deterministically retires without spawning a
    process for it, and `spark_mode=None` remains `not_required`/`ready`."""
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", _ready_uv)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "sh":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return _completed(argv)

    monkeypatch.setattr(wcp.subprocess, "run", fake_run)

    not_required = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    assert not_required["checks"]["spark"]["status"] == wcp.SPARK_NOT_REQUIRED
    assert all(argv[0] != "sh" for argv in calls)

    preferred = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations=[],
    )
    assert preferred["checks"]["spark"]["status"] == wcp.SPARK_RETIRED
    assert preferred["decision"] == wcp.DECISION_BLOCKED
    required = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations=[],
    )
    assert required["checks"]["spark"]["status"] == wcp.SPARK_RETIRED
    assert required["decision"] == wcp.DECISION_BLOCKED
    assert all(argv[0] != "sh" for argv in calls)


def test_producer_cli_without_deadline_passes_none_to_assess(monkeypatch):
    # Issue #2401 P2-2 fix_delta: renamed (no behavior change) -- this test
    # still fully replaces `assess()` and only confirms the CLI passes
    # `None` through; the sibling `test_capability_preflight_review_fixes.
    # test_assess_without_deadline_creates_one_shared_local_deadline` (AC4)
    # is the one that proves the REAL local-deadline creation behavior.
    captured = {}

    def fake_assess(**kwargs):
        captured.update(kwargs)
        return {"schema": wcp.SCHEMA}

    monkeypatch.setattr(wcp, "assess", fake_assess)
    assert wcp.main(["--profile", "issue-to-impl"]) == 0
    assert captured["deadline_ns"] is None


def test_root_transports_one_deadline_and_keeps_watchdog_grace(monkeypatch):
    clock_values = iter((1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(rer.time, "monotonic_ns", lambda: next(clock_values))
    captured = {}
    payload = {"decision": "ready", "checks": {}, "actor_capabilities": {}, "reasons": []}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs["timeout"]
        return _completed(argv, stdout=json.dumps(payload))

    monkeypatch.setattr(rer.subprocess, "run", fake_run)
    assert rer.capability_preflight_result(repo=_REPO) == payload
    deadline = int(captured["argv"][captured["argv"].index("--deadline-monotonic-ns") + 1])
    assert deadline == 31_000_000_000
    assert captured["timeout"] == wcp.DEFAULT_BUDGET_SECONDS + rer.WATCHDOG_GRACE_SECONDS


def test_watchdog_and_nonzero_stderr_taxonomy_are_distinct_and_redacted(monkeypatch):
    def timeout_expired(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(rer.subprocess, "run", timeout_expired)
    watchdog = rer.capability_preflight_result(repo=_REPO)
    assert watchdog["reasons"] == ["producer_watchdog_timeout"]

    monkeypatch.setenv("GH_TOKEN", "secret-value-that-must-not-leak")
    raw = b"\x1b[31mBearer secret-value-that-must-not-leak https://accounts.google.com/o/oauth?code=abc\x00\n"
    excerpt = rer._sanitize_stderr_excerpt(raw + b"x" * 600)
    assert len(excerpt) == rer.MAX_STDERR_EXCERPT_CHARS
    assert "secret-value-that-must-not-leak" not in excerpt
    assert "accounts.google.com" not in excerpt
    assert "\x1b" not in excerpt and "\x00" not in excerpt

    monkeypatch.setattr(
        rer.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 7, stdout="", stderr=raw),
    )
    nonzero = rer.capability_preflight_result(repo=_REPO)
    reason = nonzero["reasons"][0]
    assert reason.startswith("producer_invocation_failed:exit_7:")
    assert "secret-value-that-must-not-leak" not in reason
    assert "accounts.google.com" not in reason
