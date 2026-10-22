"""scripts/agent-guards/tests/test_trusted_runtime_capability_preflight.py

Issue #2273 AC12: `trusted_runtime_capabilities.check_trusted_uv` must not
mutate git state or the filesystem. This test drives the REAL module
against the REAL repository root (no fakes/mocks for the resolver itself)
and asserts that `git status --porcelain` and the repository's tracked
file set are byte-identical before and after the call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_GUARDS_DIR = _TESTS_DIR.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import trusted_runtime_capabilities as capabilities_mod


def _project_root() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return Path(proc.stdout.strip())


def _git_status_porcelain(project_root: Path) -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout


def test_trusted_runtime_capability_preflight_is_non_mutating():
    project_root = _project_root()
    before = _git_status_porcelain(project_root)

    result = capabilities_mod.check_trusted_uv(str(project_root))

    after = _git_status_porcelain(project_root)

    assert before == after, (
        "check_trusted_uv must not change git working tree state "
        f"(before={before!r} after={after!r})"
    )
    assert result["status"] in (
        capabilities_mod.STATUS_OK,
        capabilities_mod.STATUS_MISSING,
        capabilities_mod.STATUS_VERSION_MISMATCH,
    )
    assert "reason" in result
    assert "resolved_path" in result


def test_trusted_runtime_capability_preflight_result_has_no_credential_like_keys():
    project_root = _project_root()
    result = capabilities_mod.check_trusted_uv(str(project_root))
    assert set(result.keys()) == {"status", "reason", "diagnostic", "resolved_path"}
    for forbidden in ("token", "secret", "credential", "GH_TOKEN", "GITHUB_TOKEN"):
        assert forbidden not in str(result.get("reason", ""))


# --- Issue #2275 fix_delta P1-1: reason/diagnostic split --------------------


class _FakeRuntimeErrorModule:
    """Stand-in for `skill_runtime_exec` that raises a caller-supplied
    `RuntimeError` from `_resolve_trusted_executable`, so
    `check_trusted_uv`'s reason/diagnostic split can be tested without a
    real uv resolution failure."""

    def __init__(self, message: str) -> None:
        self._message = message

    def _resolve_trusted_executable(self, name, project_root):
        raise RuntimeError(self._message)


def test_check_trusted_uv_splits_reason_and_diagnostic_on_uv_not_found(monkeypatch):
    """GIVEN the underlying resolver raises the real
    `uv_not_found: {json}` shaped `RuntimeError`
    THEN `check_trusted_uv` must NOT forward the raw JSON-in-a-string as
    `reason` (that would double-encode JSON for consumers such as
    `workflow_capability_preflight.assess()`) -- `reason` stays a short
    stable code, and the parsed payload is exposed separately as
    `diagnostic` (Issue #2275 fix_delta P1-1)."""
    payload = {
        "error": "uv_not_found",
        "candidates_searched": ["/opt/hostedtoolcache/uv", "/fake/home/.local/bin"],
        "expected_version": "0.11.29",
        "recommended_install_dir": "/fake/home/.local/bin",
        "remediation_hint": "install the pinned uv version",
    }
    message = "uv_not_found: " + json.dumps(payload)
    monkeypatch.setattr(
        capabilities_mod, "_load_skill_runtime_exec", lambda: _FakeRuntimeErrorModule(message)
    )

    result = capabilities_mod.check_trusted_uv("/irrelevant/project/root")

    assert result["status"] == capabilities_mod.STATUS_MISSING
    assert result["reason"] == "uv_not_found"
    assert "{" not in result["reason"], "reason must never carry an embedded JSON payload"
    assert result["diagnostic"] == payload
    assert result["resolved_path"] is None


def test_check_trusted_uv_diagnostic_none_on_version_mismatch(monkeypatch):
    monkeypatch.setattr(
        capabilities_mod,
        "_load_skill_runtime_exec",
        lambda: _FakeRuntimeErrorModule("uv_version_mismatch"),
    )

    result = capabilities_mod.check_trusted_uv("/irrelevant/project/root")

    assert result["status"] == capabilities_mod.STATUS_VERSION_MISMATCH
    assert result["reason"] == "uv_version_mismatch"
    assert result["diagnostic"] is None


def test_check_trusted_uv_falls_back_gracefully_on_non_json_message(monkeypatch):
    """A legacy/non-uv-shaped `RuntimeError` message (e.g. `git_not_found`,
    or any plain string that does not match the `uv_not_found: {json}`
    shape) must fall back to `reason = original message`, `diagnostic =
    None`, rather than raising (defensive fallback required by Issue #2275
    fix_delta P1-1)."""
    monkeypatch.setattr(
        capabilities_mod,
        "_load_skill_runtime_exec",
        lambda: _FakeRuntimeErrorModule("git_not_found"),
    )

    result = capabilities_mod.check_trusted_uv("/irrelevant/project/root")

    assert result["status"] == capabilities_mod.STATUS_MISSING
    assert result["reason"] == "git_not_found"
    assert result["diagnostic"] is None


def test_check_trusted_uv_falls_back_gracefully_on_malformed_uv_not_found_json(monkeypatch):
    monkeypatch.setattr(
        capabilities_mod,
        "_load_skill_runtime_exec",
        lambda: _FakeRuntimeErrorModule("uv_not_found: {not valid json"),
    )

    result = capabilities_mod.check_trusted_uv("/irrelevant/project/root")

    assert result["status"] == capabilities_mod.STATUS_MISSING
    assert result["reason"] == "uv_not_found: {not valid json"
    assert result["diagnostic"] is None
