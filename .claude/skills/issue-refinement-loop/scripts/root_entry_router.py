#!/usr/bin/env python3
"""Capability-preflight client of the root entry router (Stage 1a-2 subset).

Only ``capability_preflight_result`` and its sanitizer are migrated: the one
function ``workflow_start_entry.py`` consumes. The transition router itself
(``run_root_transition``, the GitHub entry transports, contract-review
selection) needs GitHub mutation / review pipeline code owned by Stage 1b /
Stage 2 / I-4 and is intentionally not part of this module.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

DEFAULT_BUDGET_SECONDS = 30.0
WATCHDOG_GRACE_SECONDS = 2.0
MAX_STDERR_EXCERPT_CHARS = 500

# This local sanitizer deliberately mirrors the relevant behavior of
# `preflight_agy._redact_output_sample()` without importing a skill-local
# module across its fixture-copy boundary.
_KNOWN_CREDENTIAL_ENV_KEYS = (
    "AGY_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
_OAUTH_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*(?:accounts\.google\.com|oauth2?|/o/oauth)[^\s\"'<>]*",
    re.IGNORECASE,
)
_OAUTH_QUERY_PARAM_RE = re.compile(
    r"(?i)\b(code|state|token|access_token|refresh_token|id_token|authuser)=[^&\s\"'<>]+"
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")


def _sanitize_stderr_excerpt(value: str | bytes | None) -> str:
    """Return a redacted, display-safe stderr excerpt bounded by characters."""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value or ""
    for key in _KNOWN_CREDENTIAL_ENV_KEYS:
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b", "<redacted>", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", text)
    text = _OAUTH_URL_RE.sub("<redacted-oauth-url>", text)
    text = _OAUTH_QUERY_PARAM_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_STDERR_EXCERPT_CHARS]

_ROUTE_V1_KEYS = (
    "route",
    "reason",
    "issue_number",
    "reviewed_body_sha256",
    "observed_live_body_sha256",
    "reviewed_base_sha",
    "observed_base_sha",
    "resume_from",
    "retry_count",
)


def capability_preflight_result(
    repo: str,
    planned_operations: "tuple[dict, ...] | list[dict]" = (),
) -> dict:
    """Invoke ``scripts/agent-ops/workflow_capability_preflight.py`` once and
    return the structured ``CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1`` result
    verbatim (``decision`` / ``checks`` / ``reasons`` preserved -- Issue #2311
    P0-4/AC6/AC7). This is a module-level function (not a transport method)
    so callers such as ``workflow_start_entry.py`` can obtain the structured
    result without constructing a ``GhCliGitHubEntryTransport`` and without
    reimplementing the producer-invocation logic (Issue #2311 AC2).

    Issue #2651: the ``spark_mode``/``spark_fallback`` parameters are
    removed (GPT-5.3-Codex-Spark delegation is retired). This function's
    only callers are inside this same Issue's Allowed Paths and change
    together in this PR -- unlike ``workflow_capability_preflight.py::
    assess()``, no external pinned caller outside Allowed Paths depends on
    this function's keyword-argument shape.

    Read-only: the underlying script performs no GitHub mutation of its own
    (Issue #2273 AC12).

    Fail-closed by construction: a subprocess invocation failure (missing
    script, non-zero exit, timeout, OSError) or a malformed/non-JSON/
    non-dict stdout payload is never raised to the caller -- it is
    normalized into a synthetic ``decision: blocked`` result with a
    ``reasons`` entry describing the failure, so every caller sees the same
    3-value ``decision`` contract (``ready`` / ``degraded`` / ``blocked``)
    regardless of whether the producer itself declared ``blocked`` or the
    invocation failed outright (Issue #2311 AC3).
    """
    repo_root = Path(__file__).resolve().parents[4]
    preflight_script = (
        repo_root / "scripts" / "agent-ops" / "workflow_capability_preflight.py"
    )
    deadline_ns = time.monotonic_ns() + int(DEFAULT_BUDGET_SECONDS * 1_000_000_000)
    argv = [
        sys.executable,
        str(preflight_script),
        "--profile",
        "issue-to-impl",
        "--repo",
        repo,
        "--deadline-monotonic-ns",
        str(deadline_ns),
    ]
    planned_ops_path: Optional[str] = None
    try:
        if planned_operations:
            import tempfile

            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                prefix="planned-operations-",
                delete=False,
                encoding="utf-8",
            )
            try:
                json.dump(list(planned_operations), tmp)
            finally:
                tmp.close()
            planned_ops_path = tmp.name
            argv.extend(["--planned-operations-json", planned_ops_path])

        remaining_seconds = max(
            0.001,
            (deadline_ns - time.monotonic_ns()) / 1_000_000_000,
        )
        parent_timeout = remaining_seconds + WATCHDOG_GRACE_SECONDS
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=parent_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "decision": "blocked",
                "checks": {},
                "actor_capabilities": {},
                "reasons": ["producer_watchdog_timeout"],
            }
        except Exception as exc:  # noqa: BLE001 -- fail-closed transport boundary
            return {
                "decision": "blocked",
                "checks": {},
                "actor_capabilities": {},
                "reasons": [f"producer_invocation_failed:{exc.__class__.__name__}"],
            }
        if proc.returncode != 0:
            stderr_excerpt = _sanitize_stderr_excerpt(getattr(proc, "stderr", None))
            return {
                "decision": "blocked",
                "checks": {},
                "actor_capabilities": {},
                "reasons": [
                    f"producer_invocation_failed:exit_{proc.returncode}:{stderr_excerpt}"
                ],
            }
        try:
            result = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            return {
                "decision": "blocked",
                "checks": {},
                "actor_capabilities": {},
                "reasons": ["producer_result_malformed:non_json_stdout"],
            }
        if not isinstance(result, dict) or result.get("decision") not in (
            "ready",
            "degraded",
            "blocked",
        ):
            return {
                "decision": "blocked",
                "checks": {},
                "actor_capabilities": {},
                "reasons": ["producer_result_malformed:invalid_decision"],
            }
        return {
            "decision": result.get("decision"),
            "checks": result.get("checks", {}),
            # Issue #2340 AC2: forward the producer's actor/execution-substrate
            # -scoped capability results verbatim (empty dict default keeps this
            # backward compatible with any caller written before this Issue).
            "actor_capabilities": result.get("actor_capabilities", {}),
            "reasons": result.get("reasons", []),
        }
    finally:
        if planned_ops_path is not None:
            try:
                Path(planned_ops_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 -- best-effort temp file cleanup
                pass
