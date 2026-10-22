"""scripts/agent-guards/trusted_runtime_capabilities.py

Non-mutating, name-agnostic capability wrapper around
`skill_runtime_exec._resolve_trusted_executable` (Issue #2273 AC2/AC3/AC4/
AC12).

This module does not introduce a second trust boundary or a second version
literal: it delegates the actual resolution/validation logic to
`skill_runtime_exec`, which remains the single canonical resolver (Issue
#2241/#2251/#2276/#2280 decision record). This module exists only to give
callers *outside* `skill_runtime_exec` (e.g.
`scripts/agent-ops/workflow_capability_preflight.py`) a stable, public,
import-friendly entry point without reaching into `skill_runtime_exec`'s
private (`_`-prefixed) internals directly, and without duplicating any of
the trust-boundary logic itself.

`check_trusted_uv` performs no filesystem writes, no git operations, and no
network calls of its own -- it only resolves an executable path via
`shutil.which` (read-only PATH lookup) and, for names other than `uv`,
`skill_runtime_exec` may additionally invoke `<resolved> --version` as a
read-only subprocess to confirm the version banner (see
`_validate_trusted_executable_version` in `skill_runtime_exec.py`). Neither
step mutates repository state, so this module is safe to call from
non-mutating preflight contexts (Issue #2273 AC12).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent

# Status codes returned by `check_trusted_uv`. These intentionally mirror
# the vocabulary used by Issue #2273 AC3 ("trusted_uv_missing" /
# "trusted_uv_version_mismatch") rather than re-exposing
# `skill_runtime_exec`'s internal `RuntimeError` message strings verbatim,
# so callers have a small, stable enum instead of a set of implementation
# detail strings that could change if `skill_runtime_exec` is refactored.
STATUS_OK = "ok"
STATUS_MISSING = "trusted_uv_missing"
STATUS_VERSION_MISMATCH = "trusted_uv_version_mismatch"


def _load_skill_runtime_exec():
    """Import `skill_runtime_exec` as a plain module (not a package --
    `scripts/agent-guards` has no `__init__.py`, matching the existing
    import pattern used by
    `scripts/agent-guards/tests/test_trusted_toolchain_isolated_home.py`)."""
    guards_dir_str = str(_GUARDS_DIR)
    inserted = guards_dir_str not in sys.path
    if inserted:
        sys.path.insert(0, guards_dir_str)
    try:
        import skill_runtime_exec  # type: ignore

        return skill_runtime_exec
    finally:
        if inserted:
            try:
                sys.path.remove(guards_dir_str)
            except ValueError:
                pass


def check_trusted_uv(project_root: str) -> dict:
    """Resolve `uv` via the canonical trust boundary and report a
    structured, JSON-serializable result.

    Returns a dict with keys:
      - status: one of `STATUS_OK` / `STATUS_MISSING` / `STATUS_VERSION_MISMATCH`
      - reason: short, stable, machine-readable reason CODE (never contains
        credential material, and never a raw JSON-encoded string -- this
        function only ever handles executable paths and version banners,
        never tokens or secrets). Consumers such as
        `scripts/agent-ops/workflow_capability_preflight.py` project this
        value directly into `CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1.checks.uv.reason`,
        so it must stay a short stable string, not a JSON blob (Issue #2275
        fix_delta P1-1: `skill_runtime_exec._resolve_trusted_executable`'s
        `uv_not_found` `RuntimeError` message is itself
        `"uv_not_found: " + json.dumps(payload)` -- forwarding `str(exc)`
        verbatim as `reason` would double-encode JSON inside a JSON string
        for this consumer).
      - diagnostic: structured object (same keys as
        `skill_runtime_exec`'s `uv_not_found` diagnostic payload: `error`,
        `candidates_searched`, `expected_version`, `recommended_install_dir`,
        `remediation_hint`) when the underlying `RuntimeError` carried a
        parseable `uv_not_found: {json}` payload, else `None` (e.g. for
        `uv_version_mismatch`, or for any legacy/plain-string `RuntimeError`
        message that does not match the expected shape).
      - resolved_path: the trust-validated absolute path to `uv`, or None
        if resolution failed
    """
    mod = _load_skill_runtime_exec()
    try:
        resolved = mod._resolve_trusted_executable("uv", project_root)
    except RuntimeError as exc:
        message = str(exc)
        if message == "uv_version_mismatch":
            return {
                "status": STATUS_VERSION_MISMATCH,
                "reason": message,
                "diagnostic": None,
                "resolved_path": None,
            }
        reason, diagnostic = _split_uv_not_found_message(message)
        return {
            "status": STATUS_MISSING,
            "reason": reason,
            "diagnostic": diagnostic,
            "resolved_path": None,
        }
    return {
        "status": STATUS_OK,
        "reason": "resolved",
        "diagnostic": None,
        "resolved_path": resolved,
    }


_UV_NOT_FOUND_PREFIX = "uv_not_found: "


def _split_uv_not_found_message(message: str) -> tuple[str, dict | None]:
    """Split a `skill_runtime_exec` `RuntimeError` message into a stable
    `reason` code and an optional structured `diagnostic` payload.

    `skill_runtime_exec._resolve_trusted_executable("uv", ...)` raises
    `RuntimeError("uv_not_found: " + json.dumps(payload))` on resolution
    failure. This helper strips the `"uv_not_found: "` prefix and parses the
    JSON payload exactly once, so callers of `check_trusted_uv` never
    receive a JSON string embedded inside another JSON string (Issue #2275
    fix_delta P1-1).

    Defensive fallback: if `message` does not start with the expected
    prefix, or the suffix is not valid JSON, this returns
    `(message, None)` unchanged rather than raising -- this preserves
    behavior for any non-uv-shaped or legacy-format `RuntimeError` message.
    """
    if not message.startswith(_UV_NOT_FOUND_PREFIX):
        return message, None
    payload_text = message[len(_UV_NOT_FOUND_PREFIX) :]
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, ValueError):
        return message, None
    if not isinstance(payload, dict):
        return message, None
    error_code = payload.get("error")
    reason = error_code if isinstance(error_code, str) and error_code else "uv_not_found"
    return reason, payload
