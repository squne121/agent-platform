""".claude/skills/issue-refinement-loop/scripts/tests/test_root_entry_router_workflow_capability.py

Issue #2273 AC15/AC16: `root_entry_router.GhCliGitHubEntryTransport.
capability_preflight()` must consume the structured
`CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1` result produced by
`scripts/agent-ops/workflow_capability_preflight.py` (AC15), and a
positive fixture (native GitHub auth + repository read + trusted `uv`
available, no required Spark failure) must let `run_root_transition`
advance to the fresh-review phase exactly once (AC16).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import root_entry_router as rer  # noqa: E402


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

_REPO_ROOT = Path(__file__).resolve().parents[5]
_FAKE_GH_FIXTURE = _REPO_ROOT / "scripts" / "agent-ops" / "tests" / "fixtures" / "fake_gh.py"


def _fake_workflow_capability_result(decision: str) -> str:
    return json.dumps(
        {
            "schema": "CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1",
            "profile": "issue-to-impl",
            "decision": decision,
            "checks": {
                "uv": {"status": "ok", "reason": "resolved"},
                "spark": {"status": "not_required"},
                "github": {"auth": True, "repo_read": True, "operations": {}},
            },
            "reasons": [],
        }
    )


# --- AC15 --------------------------------------------------------------




# --- AC16 ----------------------------------------------------------------




# --- P0-2: capability-preflight-blocked must not publish an audit comment ---




# --- P0-3: production consumer declares its own planned audit mutation ---









# --- Generic result-field passthrough (PR #2372 REQUEST_CHANGES follow-up) ---


def test_capability_preflight_result_forwards_generic_result_fields(monkeypatch):
    """GIVEN the producer (workflow_capability_preflight.py) returns an
    arbitrary/generic result payload
    WHEN capability_preflight_result() is called
    THEN it forwards the producer's `decision` / `checks` /
    `actor_capabilities` / `reasons` fields through unchanged -- verified
    with generic sentinel names, not AGY-specific ones (PR #2372
    REQUEST_CHANGES P2: a positive contract test for the transparent
    passthrough behavior of `capability_preflight_result()`)."""

    payload = {
        "decision": "ready",
        "checks": {"sentinel_check": {"status": "ok"}},
        "actor_capabilities": {
            "sentinel_actor": {
                "status": "ready",
                "reason_code": None,
            }
        },
        "reasons": ["sentinel_reason"],
    }

    monkeypatch.setattr(
        rer.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = rer.capability_preflight_result(repo=_REPO)

    assert result == payload


# --- P1-4: real producer/consumer integration via a fake `gh` on PATH -----


def _install_fake_gh(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "gh"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} "{_FAKE_GH_FIXTURE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def test_real_producer_blocked_on_fake_gh_auth_failure(tmp_path, monkeypatch):
    """Real producer subprocess (fake `gh` on PATH only): a failing `gh auth status`
    yields decision=blocked through the real capability_preflight_result()."""
    bin_dir = tmp_path / "bin"
    _install_fake_gh(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))
    monkeypatch.setenv("FAKE_GH_AUTH_OK", "0")

    result = rer.capability_preflight_result(repo=_REPO)

    assert result["decision"] == "blocked"
    assert result["checks"]["github"]["auth"] is False


def test_real_producer_receives_planned_operations(tmp_path, monkeypatch):
    """Real producer subprocess (fake `gh` on PATH only): an unregistered mutation operation
    reaches the producer through --planned-operations-json and makes it blocked."""
    bin_dir = tmp_path / "bin"
    _install_fake_gh(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))
    monkeypatch.setenv("FAKE_GH_AUTH_OK", "1")
    monkeypatch.setenv("FAKE_GH_REPO_READ_OK", "1")

    result = rer.capability_preflight_result(
        repo=_REPO,
        planned_operations=(
            {
                "phase": "audit",
                "actor_role": "root_entry_router",
                "operation": "an_unregistered_operation_for_p1_4",
                "requires_mutation": True,
            },
        ),
    )

    assert result["decision"] == "blocked"
    assert "an_unregistered_operation_for_p1_4" in result["checks"]["github"]["operations"]
