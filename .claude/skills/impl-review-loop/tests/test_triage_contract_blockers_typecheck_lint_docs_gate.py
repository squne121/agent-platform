"""
Tests for Issue #1511: triage_contract_blockers.py canonical pnpm gate expansion.

AC10: pnpm typecheck:e2e / pnpm lint:docs no-TTY blockers are classified as
      retry_with_runner_env_delta (not non_canonical_pnpm_gate) by
      triage_contract_blockers.py's _CANONICAL_PNPM_GATES allowlist.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT_PATH = HERE.parent / "scripts" / "triage_contract_blockers.py"

spec = importlib.util.spec_from_file_location("triage_contract_blockers", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def make_item(
    ac: str,
    category: str,
    *,
    command_hash: str = "sha256:" + "a" * 64,
    exit_code: int = 1,
    raw_command: str | None = None,
    runner_env_delta: dict | None = None,
    subreason: str | None = None,
) -> dict:
    item = {
        "ac": ac,
        "command_hash": command_hash,
        "category": category,
        "decision": "blocked",
        "exit_code": exit_code,
        "runner_env_delta": runner_env_delta or {},
    }
    if raw_command is not None:
        item["raw_command"] = raw_command
    if subreason is not None:
        item["subreason"] = subreason
    return item


def test_pnpm_typecheck_e2e_gate_action():
    """AC10: pnpm typecheck:e2e no-TTY → retry_with_runner_env_delta with argv=['pnpm', 'typecheck:e2e']"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC1",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm typecheck:e2e",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok", f"Expected ok, got {result['status']}: {result.get('errors')}"
    action = result["suggested_actions"][0]
    assert action["kind"] == "retry_with_runner_env_delta", (
        f"Expected retry_with_runner_env_delta, got {action['kind']}"
    )
    assert action["argv"] == ["pnpm", "typecheck:e2e"], (
        f"Expected argv=['pnpm', 'typecheck:e2e'], got {action['argv']!r}"
    )
    assert action["env_delta"] == {"CI": "true"}


def test_pnpm_lint_docs_gate_action():
    """AC10: pnpm lint:docs no-TTY → retry_with_runner_env_delta with argv=['pnpm', 'lint:docs']"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC2",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm lint:docs",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok", f"Expected ok, got {result['status']}: {result.get('errors')}"
    action = result["suggested_actions"][0]
    assert action["kind"] == "retry_with_runner_env_delta"
    assert action["argv"] == ["pnpm", "lint:docs"], (
        f"Expected argv=['pnpm', 'lint:docs'], got {action['argv']!r}"
    )
    assert action["env_delta"] == {"CI": "true"}


def test_pnpm_typecheck_e2e_not_non_canonical():
    """AC10: pnpm typecheck:e2e must NOT be rejected as non_canonical_pnpm_gate."""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC1",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm typecheck:e2e",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert "non_canonical_pnpm_gate" not in result.get("errors", [])
    retry_actions = [
        a for a in result.get("suggested_actions", []) if a.get("kind") == "retry_with_runner_env_delta"
    ]
    assert len(retry_actions) == 1


def test_pnpm_lint_docs_not_non_canonical():
    """AC10: pnpm lint:docs must NOT be rejected as non_canonical_pnpm_gate."""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC2",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm lint:docs",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert "non_canonical_pnpm_gate" not in result.get("errors", [])
    retry_actions = [
        a for a in result.get("suggested_actions", []) if a.get("kind") == "retry_with_runner_env_delta"
    ]
    assert len(retry_actions) == 1


def test_pnpm_typecheck_e2e_reason_string():
    """AC10: reason string references pnpm typecheck:e2e (not pnpm build)."""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC1",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm typecheck:e2e",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    action = result["suggested_actions"][0]
    assert "pnpm typecheck:e2e" in action["reason"]
    assert "pnpm build" not in action["reason"]


def test_pnpm_typecheck_e2e_with_extra_args_is_not_canonical():
    """Negative: raw_command='pnpm typecheck:e2e --filter foo' must not produce retry_with_runner_env_delta."""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC1",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm typecheck:e2e --filter foo",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    retry_actions = [
        a for a in result.get("suggested_actions", []) if a.get("kind") == "retry_with_runner_env_delta"
    ]
    assert len(retry_actions) == 0, (
        f"'pnpm typecheck:e2e --filter foo' (extra args) must not produce retry_with_runner_env_delta; "
        f"got actions: {result.get('suggested_actions', [])}"
    )
