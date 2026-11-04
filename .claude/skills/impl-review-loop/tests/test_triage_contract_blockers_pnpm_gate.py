"""
Tests for Issue #994: triage_contract_blockers.py gate-specific argv for pnpm no-TTY actions.

AC9: triage consumer が pnpm lint / pnpm typecheck の no-TTY 失敗時に
     pnpm build ではなく failing gate の argv を提案する
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


# ---------------------------------------------------------------------------
# AC9: failing gate argv is returned instead of hardcoded pnpm build
# ---------------------------------------------------------------------------

def test_pnpm_lint_gate_action():
    """AC9: pnpm lint no-TTY → suggested_action.argv == ['pnpm', 'lint'] (not ['pnpm', 'build'])"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC2",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm lint",
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
    assert action["argv"] == ["pnpm", "lint"], (
        f"Expected argv=['pnpm', 'lint'] for pnpm lint gate, got {action['argv']!r}"
    )
    assert action["env_delta"] == {"CI": "true"}, f"Expected CI=true env_delta, got {action['env_delta']}"


def test_pnpm_typecheck_gate_action():
    """AC9: pnpm typecheck no-TTY → suggested_action.argv == ['pnpm', 'typecheck'] (not ['pnpm', 'build'])"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC1",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm typecheck",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok", f"Expected ok, got {result['status']}: {result.get('errors')}"
    action = result["suggested_actions"][0]
    assert action["kind"] == "retry_with_runner_env_delta"
    assert action["argv"] == ["pnpm", "typecheck"], (
        f"Expected argv=['pnpm', 'typecheck'] for pnpm typecheck gate, got {action['argv']!r}"
    )
    assert action["env_delta"] == {"CI": "true"}


def test_pnpm_test_gate_action():
    """AC9: pnpm test no-TTY → suggested_action.argv == ['pnpm', 'test'] (not ['pnpm', 'build'])"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC3",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm test",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok", f"Expected ok, got {result['status']}: {result.get('errors')}"
    action = result["suggested_actions"][0]
    assert action["kind"] == "retry_with_runner_env_delta"
    assert action["argv"] == ["pnpm", "test"], (
        f"Expected argv=['pnpm', 'test'] for pnpm test gate, got {action['argv']!r}"
    )
    assert action["env_delta"] == {"CI": "true"}


def test_pnpm_build_gate_action_unchanged():
    """AC9/AC4: pnpm build no-TTY → suggested_action.argv == ['pnpm', 'build'] (existing behavior maintained)"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC4",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm build",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok", f"Expected ok, got {result['status']}: {result.get('errors')}"
    action = result["suggested_actions"][0]
    assert action["kind"] == "retry_with_runner_env_delta"
    assert action["argv"] == ["pnpm", "build"], (
        f"Expected argv=['pnpm', 'build'] for pnpm build gate, got {action['argv']!r}"
    )
    assert action["env_delta"] == {"CI": "true"}


def test_pnpm_gate_fallback_when_no_raw_command():
    """AC9: when raw_command is absent, fallback to ['pnpm', 'build'] for backward compat"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC3",
                "package_manager_no_tty_prompt",
                exit_code=1,
                # raw_command intentionally absent
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok", f"Expected ok, got {result['status']}: {result.get('errors')}"
    action = result["suggested_actions"][0]
    assert action["kind"] == "retry_with_runner_env_delta"
    # Fallback to ["pnpm", "build"] when raw_command is absent
    assert action["argv"] == ["pnpm", "build"], (
        f"Expected fallback argv=['pnpm', 'build'] when raw_command absent, got {action['argv']!r}"
    )


def test_pnpm_lint_gate_action_reason_string():
    """AC9: reason string for pnpm lint gate references pnpm lint (not pnpm build)"""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC2",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm lint",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    action = result["suggested_actions"][0]
    assert "pnpm lint" in action["reason"], (
        f"Expected reason to reference 'pnpm lint', got: {action['reason']!r}"
    )
    assert "pnpm build" not in action["reason"], (
        f"reason must not reference 'pnpm build' for a pnpm lint gate, got: {action['reason']!r}"
    )


# ---------------------------------------------------------------------------
# Negative tests: non-canonical pnpm commands must NOT return retry_with_runner_env_delta
# ---------------------------------------------------------------------------

def test_pnpm_install_is_not_canonical_gate():
    """Negative: raw_command='pnpm install' must not return retry_with_runner_env_delta."""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC1",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm install",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    # The non-canonical item must be rejected, causing no accepted items
    actions = result.get("suggested_actions", [])
    retry_actions = [a for a in actions if a.get("kind") == "retry_with_runner_env_delta"]
    assert len(retry_actions) == 0, (
        f"'pnpm install' must not produce retry_with_runner_env_delta; got actions: {actions}"
    )


def test_pnpm_lint_with_extra_args_is_not_canonical():
    """Negative: raw_command='pnpm lint --filter foo' must not return retry_with_runner_env_delta."""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC2",
                "package_manager_no_tty_prompt",
                exit_code=1,
                raw_command="pnpm lint --filter foo",
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    actions = result.get("suggested_actions", [])
    retry_actions = [a for a in actions if a.get("kind") == "retry_with_runner_env_delta"]
    assert len(retry_actions) == 0, (
        f"'pnpm lint --filter foo' (extra args) must not produce retry_with_runner_env_delta; "
        f"got actions: {actions}"
    )


def test_pnpm_fallback_when_no_raw_command_returns_build_argv():
    """Negative complement: raw_command absent uses legacy fallback ['pnpm', 'build'], not non-canonical rejection."""
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            make_item(
                "AC3",
                "package_manager_no_tty_prompt",
                exit_code=1,
                # raw_command intentionally absent — only the legacy fallback path
                runner_env_delta={},
            )
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["status"] == "ok", (
        f"raw_command absent must use legacy fallback, not fail; got {result['status']}: {result.get('errors')}"
    )
    actions = result.get("suggested_actions", [])
    retry_actions = [a for a in actions if a.get("kind") == "retry_with_runner_env_delta"]
    assert len(retry_actions) == 1, (
        f"raw_command absent must produce exactly one retry_with_runner_env_delta; got: {retry_actions}"
    )
    assert retry_actions[0]["argv"] == ["pnpm", "build"], (
        f"Legacy fallback must use ['pnpm', 'build']; got {retry_actions[0]['argv']!r}"
    )
