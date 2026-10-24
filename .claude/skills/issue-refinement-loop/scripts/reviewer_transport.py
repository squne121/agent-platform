#!/usr/bin/env python3
"""Parent-owned, fail-closed transport for issue-reviewer results.

The V2 compact wire is deliberately small, but it is *not* the authority for
the review result.  The parent creates an immutable attempt directory before
starting a child, records transport facts there, and accepts a semantic result
only after the same raw artifact bytes have passed all binding checks.

This module is the V2 contract owner.  Callers must not reimplement its
grammar, artifact layout, or retry policy.

PR #2142 owner REQUEST_CHANGES (P0-1/P0-2/P1-1/P1-2/P1-3): this revision adds
(a) ``build_backend_command()``, a single backend command adapter that
materializes same-session/fresh-session resume identity into real argv per
attempt (the child command is no longer identical across retries), (b)
``project_compact_v2_from_artifact()`` / ``verify_wire_matches_artifact()``,
which re-derive the canonical wire from a verified artifact's
``semantic_result`` so a caller can prove a (possibly separately relayed)
wire string still reflects that artifact -- hash/schema/repo/issue/body/
invocation/attempt binding on the artifact bytes alone does not prove this,
(c) a hard post-wait total-deadline check (late-result rejection), (d) a
bounded reader-thread join with an escaped-grandchild fault path, (e) an
exact ``os.open in os.supports_dir_fd`` capability gate, and (f) a
dir-fd-anchored, component-wise no-follow *write* path shared by every
producer so a symlinked intermediate directory cannot be silently followed
on create either.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Sibling/cross-skill module imports (Issue #2242 OWNER adversarial review,
# source-repo PR #2246 comment 5328161000,
# Blocker 4): reuse the canonical owner/repo format regex already defined by
# `command_registry.py` (same `scripts/` directory) and the canonical
# `REVIEW_ISSUE_RESULT_V1` jsonschema validator already defined by
# `review-issue/scripts/check_issue_contract.py`, instead of reimplementing
# either as a second, independently drifting copy.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from command_registry import _OWNER_REPO_RE as _CANONICAL_OWNER_REPO_RE  # noqa: E402

_REPO_ROOT = _SCRIPTS_DIR.parent.parent.parent.parent
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
if str(_REVIEW_ISSUE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REVIEW_ISSUE_SCRIPTS))
import check_issue_contract as _check_issue_contract  # noqa: E402

SCHEMA_V2 = "ISSUE_REVIEW_RESULT_COMPACT_V2"
ATTEMPT_SCHEMA = "REVIEWER_ATTEMPT_RESULT_V1"
MANIFEST_SCHEMA = "REVIEWER_ATTEMPT_MANIFEST_V1"
ARTIFACT_SCHEMA = "REVIEWER_COMPACT_ARTIFACT_V2"
MAX_ATTEMPTS = 3
# Issue #2165 (OWNER 2026-08-15 REQUEST_CHANGES, P1-1): these two constants
# previously assumed every attempt's wall time was governed only by
# transport-level concerns (spawn/signal/hang detection). In production the
# `deterministic` backend's single child process
# (`run_root_review_pipeline.py run-checker-attempt`) also executes the
# real checker chain synchronously inside that same attempt window --
# `check_issue_contract.py` -> `contract_readiness_check.py` (dominated by
# `baseline_vc_preflight.py` actually *running* every Verification Command
# in the reviewed Issue body) -> `merge_readiness`. The old
# PER_ATTEMPT_DEADLINE_SECONDS=90 could not fit even the single dominant VC
# call, so any Issue with a legitimately long-running VC deterministically
# timed out on every attempt; a later fix (PER_ATTEMPT=300/TOTAL=340) still
# undershot the layered ceiling those three subprocess calls' OWN
# independently-derived timeouts add up to (30 + 250 + 30 = 310 > 300).
#
# These module-level constants are now only a GENERIC FALLBACK for callers
# that do not pass an explicit `per_attempt_deadline`/`total_deadline`.
# `run_root_review_pipeline.py`'s `_cmd_produce()` (the deterministic
# backend's actual caller) derives and passes its OWN explicit values from
# `contract_readiness_check.CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS` (which
# itself derives from `baseline_vc_preflight.py`'s per-VC-command cap, see
# that module's Issue #2165 comments) plus the sibling
# `check_issue_contract`/`merge_readiness` budgets and a process-spawn
# margin, so the SAME arithmetic-break class of bug cannot recur here
# independently of the value chosen below. The fallback values below are
# kept generously above the pre-#2165-fix layered ceiling (310s) so a
# caller that forgets to pass explicit deadlines still fails safe rather
# than reintroducing the original bug.
#
# Retry policy (OWNER 2026-08-14/2026-08-15 review): resubmitting the SAME
# deterministic checker command against the SAME pinned Issue body cannot
# change the outcome of a `timeout` classification -- the VC itself is what
# is over budget, not the process spawn/IPC layer -- so `retry_matrix()`
# below excludes `timeout` from the deterministic backend's retryable set
# (P1-3: the deterministic backend's retryable set is now a closed
# allowlist of transport-layer-only reason codes -- `spawn_failure`,
# `signal`, `capture_failure` -- not "every reason_code except timeout").
# This means the deterministic backend effectively gets ONE full-length
# attempt for a VC-execution-budget failure. Other backends (`claude`) keep
# retrying on timeout, since an LLM session hang is plausibly transient in a
# way a deterministic subprocess budget overrun is not.
#
# TOTAL_DEADLINE_SECONDS stays >= PER_ATTEMPT_DEADLINE_SECONDS (+40s margin)
# so a single full-length attempt is never starved of its own per-attempt
# budget by `min(per_attempt_deadline, total_deadline - elapsed)`; the
# margin covers a small number of fast (`spawn_failure` etc.) retries that
# remain possible for other reason codes, bounded further by
# `MIN_RETRY_ATTEMPT_BUDGET_FRACTION` below (P1-1 marge condition 8): a
# retry is never spawned once the time remaining before `total_deadline`
# drops below that fraction of the PER-ATTEMPT budget.
PER_ATTEMPT_DEADLINE_SECONDS = 480
TOTAL_DEADLINE_SECONDS = 520
# Issue #2165 P1-1 marge condition 8: the minimum time budget that must
# remain before `total_deadline`, expressed as a FRACTION of
# `per_attempt_deadline` (not an absolute constant), for a NEW (2nd/3rd)
# attempt to be spawned at all -- covers process spawn + a minimal useful
# execution slice + cleanup. A fraction (rather than a fixed number of
# seconds) scales correctly across very different `per_attempt_deadline`
# magnitudes: the deterministic backend's real ~480s per-attempt budget
# needs tens of seconds of headroom to be worth retrying, while a
# short-deadline caller (e.g. a test using a 1s per-attempt budget) needs
# only a proportionally small headroom, not the same fixed floor. If the
# remaining total-deadline budget is smaller than this fraction of the
# per-attempt budget, spawning another attempt could not possibly complete
# real work, so `run_reviewer_transport()` stops retrying instead of
# unconditionally spawning a bounded-to-fail attempt.
MIN_RETRY_ATTEMPT_BUDGET_FRACTION = 0.1
STDOUT_CAP = 65_536
# Issue #2165 merge condition #8 (PR #2177 OWNER 2026-08-15 REQUEST_CHANGES,
# fix_delta iteration 2): `has_sufficient_retry_attempt_budget()` is the
# SINGLE, backend-agnostic budget check that gates spawning ANY new attempt
# (2nd/3rd, of ANY backend -- deterministic, claude, fixture) after
# the first. `run_reviewer_transport()`'s own attempt loop already applies
# this uniformly regardless of `backend` (there is no `if backend ==
# "deterministic"` branch around the check itself -- only `retry_matrix()`'s
# retryable-reason-code allowlist differs per backend). The gap this
# iteration closes is a SEPARATE, OUTER retry layer:
# `run_root_review_pipeline.py`'s `retry_once_on_transport_failure()`, which
# previously retried `invoke_child()` unconditionally exactly once on a
# transport failure with NO remaining-budget awareness at all (a different
# retry loop from this module's `run_reviewer_transport()` attempt loop,
# and it had no way to reject a retry regardless of how little
# total-deadline budget remained). This helper is exported so that other
# in-process retry callers (in Allowed Paths) share the SAME guard rather
# than a second, independently hand-picked policy.
def has_sufficient_retry_attempt_budget(
    *,
    elapsed_seconds: float,
    total_deadline_seconds: float,
    per_attempt_deadline_seconds: float,
    min_fraction: float = MIN_RETRY_ATTEMPT_BUDGET_FRACTION,
) -> bool:
    """Return whether a NEW attempt (of any backend) is worth spawning.

    ``False`` when the time remaining before ``total_deadline_seconds`` is
    smaller than ``min_fraction * per_attempt_deadline_seconds`` -- too
    small for a new attempt to realistically spawn, do minimal useful work,
    and clean up. Callers MUST NOT spawn a new attempt when this returns
    ``False``, regardless of backend: a non-deterministic (claude) backend
    attempt has no better chance of completing useful work in a near-zero
    remaining budget than a deterministic one does.
    """
    remaining_seconds = total_deadline_seconds - elapsed_seconds
    return remaining_seconds >= per_attempt_deadline_seconds * min_fraction



STDERR_CAP = 262_144
ARTIFACT_MAX_BYTES = 1_048_576
READER_JOIN_GRACE_SECONDS = 5
REAP_CONFIRM_ATTEMPTS = 5
REAP_CONFIRM_INTERVAL_SECONDS = 0.2
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

V2_FIELDS = (
    "SCHEMA",
    "STATUS",
    "VERDICT",
    "SUMMARY",
    "BLOCKERS",
    "NEXT_ACTION",
    "MUST_READ",
    "REVIEWED_BODY_SHA256",
    "ATTEMPT_ID",
    "ARTIFACT",
    "ARTIFACT_SHA256",
)


def canonical_json_bytes(value: Any) -> bytes:
    """#2068 parity: deterministic UTF-8 JSON digest preimage."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_prefixed(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def generate_invocation_id() -> str:
    return str(uuid.uuid4())


def strict_json_loads(raw: bytes | str) -> Any:
    """Parse JSON while rejecting duplicate members and non-finite constants."""
    text = raw.decode("utf-8", "strict") if isinstance(raw, bytes) else raw

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(
        text, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("non_finite_json"))
    )


def _relative_parts(path: str) -> tuple[str, ...]:
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError("artifact_path_not_relative")
    return candidate.parts


def artifact_relative_path(issue_number: int, invocation_id: str, attempt: int) -> str:
    if issue_number <= 0 or attempt <= 0 or not _ATTEMPT.match(invocation_id):
        raise ValueError("invalid_artifact_identity")
    return f"{issue_number}/{invocation_id}/attempt-{attempt:03d}/compact_review_result_v2.json"


def attempt_relative_dir(issue_number: int, invocation_id: str, attempt: int) -> str:
    return str(Path(artifact_relative_path(issue_number, invocation_id, attempt)).parent)


# ---------------------------------------------------------------------------
# Secure, dir-fd-anchored no-follow open (shared capability gate)
# ---------------------------------------------------------------------------


def _has_secure_open_capability() -> bool:
    """Exact capability gate (PR #2142 owner REQUEST_CHANGES P1-2).

    ``os.supports_dir_fd`` is a *set of functions* that accept the ``dir_fd``
    keyword on this platform, not a bool.  The previous ``not
    os.supports_dir_fd`` check only tested truthiness of that set, so it
    passed as long as *any* function supported ``dir_fd`` -- not
    specifically ``os.open`` (the function this module actually calls with
    ``dir_fd=``).  Python's own documentation defines membership testing as
    ``os.open in os.supports_dir_fd``.
    """
    return (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "open")
        and os.open in os.supports_dir_fd
        and hasattr(os, "mkdir")
        and os.mkdir in os.supports_dir_fd
    )


def _open_no_follow(root: Path, relative: str) -> tuple[int, int]:
    """Open root and each path component by FD; returns (root_fd, leaf_fd)."""
    if not _has_secure_open_capability():
        raise OSError("unsupported_secure_open_capability")
    parts = _relative_parts(relative)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = root_fd
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            if current != root_fd:
                os.close(current)
            current = next_fd
        # Issue #2242 OWNER adversarial review Blocker 3
        # (source-repo PR #2246 comment 5328161000):
        # `O_NONBLOCK` on the leaf open prevents an indefinite hang (a real
        # denial-of-service risk, not merely a correctness gap) if a FIFO
        # (named pipe) with no writer is substituted for the artifact path --
        # POSIX `open()` on a read-only FIFO blocks until a writer opens it
        # UNLESS `O_NONBLOCK` is set, in which case it returns immediately.
        # `O_NONBLOCK` has no effect on regular-file open/read semantics, so
        # this is safe for the legitimate regular-file case; the subsequent
        # `stat.S_ISREG()` check in `secure_read_json()` rejects the FIFO.
        leaf_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        if current != root_fd:
            os.close(current)
        return root_fd, leaf_fd
    except BaseException:
        if current != root_fd:
            os.close(current)
        os.close(root_fd)
        raise


def _mkdir_no_follow(root: Path, relative_dir: str) -> None:
    """Create every intermediate directory component of ``relative_dir``
    under ``root`` via dir-fd-anchored, component-wise no-follow open/mkdir
    (PR #2142 owner REQUEST_CHANGES P1-3): a producer using plain
    ``Path.mkdir(parents=True)`` + a separate ``os.open()`` on the combined
    path would silently follow a pre-existing symlink placed at any
    intermediate component -- the exact resolve/open split AC6 forbids on
    the consumer side, which previously had no producer-side counterpart.
    Existing components are opened (not re-created); a component that
    exists but is not a real directory (e.g. a symlink) is rejected.
    """
    if not _has_secure_open_capability():
        raise OSError("unsupported_secure_open_capability")
    # `root` itself is a trusted, caller-controlled anchor (not attacker
    # influenced the way relative path components can be) -- e.g. the
    # canonical `.claude/artifacts/issue-refinement-loop` base. Ensuring it
    # exists is a plain mkdir; the dir-fd-anchored no-follow treatment below
    # applies to every component *under* it, which is the actual boundary
    # this function defends.
    root.mkdir(parents=True, exist_ok=True)
    if not relative_dir or relative_dir == ".":
        return
    parts = Path(relative_dir).parts
    if not parts or any(part in ("..",) for part in parts):
        raise ValueError("artifact_path_not_relative")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = root_fd
    try:
        for component in parts:
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            if current != root_fd:
                os.close(current)
            current = next_fd
    finally:
        if current != root_fd:
            os.close(current)
        os.close(root_fd)


def _atomic_create(root: Path, relative: str, content: bytes) -> Path:
    """Create ``root/relative`` exactly once (``O_CREAT|O_EXCL``), anchored
    via dir-fd component-wise no-follow traversal from ``root`` for both the
    intermediate directories and the leaf file (PR #2142 owner
    REQUEST_CHANGES P1-3). No unsafe fallback: platforms without the exact
    ``os.open in os.supports_dir_fd`` capability fail closed.
    """
    root.mkdir(parents=True, exist_ok=True)
    parts = _relative_parts(relative)
    parent_relative = str(Path(*parts[:-1])) if len(parts) > 1 else ""
    _mkdir_no_follow(root, parent_relative)
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = parent_fd
        for component in parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            if current != parent_fd:
                os.close(current)
            current = next_fd
        leaf_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        leaf_fd = os.open(parts[-1], leaf_flags, 0o600, dir_fd=current)
        try:
            with os.fdopen(leaf_fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if current != parent_fd:
                os.close(current)
    finally:
        os.close(parent_fd)
    return root.joinpath(*parts)


def _bounded_reader(stream: Any, cap: int, bucket: dict[str, Any]) -> None:
    digest = hashlib.sha256()
    prefix = bytearray()
    suffix = bytearray()
    length = 0
    while True:
        block = stream.read(8192)
        if not block:
            break
        length += len(block)
        digest.update(block)
        if len(prefix) < cap:
            prefix.extend(block[: cap - len(prefix)])
        suffix.extend(block)
        if len(suffix) > cap:
            del suffix[:-cap]
    bucket.update(length=length, sha256="sha256:" + digest.hexdigest(), prefix=bytes(prefix), suffix=bytes(suffix))


def _redact_bytes(raw: bytes) -> str:
    # The values are diagnostic-only and capped.  Keep no env/prompt/secret
    # material in receipt telemetry.
    return "<redacted:%d-bytes>" % len(raw)


def _confirm_process_group_reaped(pgid: int) -> bool:
    """Best-effort confirmation that no process remains in ``pgid`` (PR
    #2142 owner REQUEST_CHANGES P1-1): ``descendants_reaped`` must reflect an
    observation, not merely ``timed_out``.  Reaps any zombie children of
    *this* process in ``pgid`` via ``os.waitpid(-pgid, WNOHANG)`` so an
    escaped grandchild that re-parented to init is not falsely counted as
    reaped, then probes liveness with signal 0.
    """
    for _ in range(REAP_CONFIRM_ATTEMPTS):
        try:
            while True:
                reaped_pid, _status = os.waitpid(-pgid, os.WNOHANG)
                if reaped_pid == 0:
                    break
        except ChildProcessError:
            pass
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(REAP_CONFIRM_INTERVAL_SECONDS)
    return False


def _make_manifest(
    *,
    issue_number: int,
    repo: str,
    invocation_id: str,
    attempt: int,
    backend: str,
    session_ref: str | None,
    retry_intent: str,
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "issue_number": issue_number,
        "repository": repo,
        "invocation_id": invocation_id,
        "attempt": attempt,
        "backend": backend,
        "session_ref": session_ref,
        "retry_intent": retry_intent,
        "transport_status": "started",
    }


def _write_json_once(root: Path, relative: str, value: dict[str, Any]) -> Path:
    return _atomic_create(root, relative, canonical_json_bytes(value))


@dataclass(frozen=True)
class RetryIntent:
    kind: str
    session_ref: str | None


# Issue #2165 P1-3 (OWNER 2026-08-15 REQUEST_CHANGES): for the
# `deterministic` backend, retrying a checker attempt only makes sense for
# a failure_class that is plausibly TRANSIENT for the exact same argv
# against the exact same pinned Issue body -- process spawn failing to
# launch, this process itself being killed by a signal, or this module's
# own bounded pipe-reader thread failing to observe EOF in time
# (`capture_failure`; a real signal/hang class, not a data-shape problem).
# A CLOSED allowlist (not "every reason_code except timeout") because the
# previous "subtract timeout/inner_timeout from everything" design left
# `nonzero_exit` / `malformed_output` / `empty_output` /
# `artifact_validation_failure` retryable for `deterministic` even though
# none of those can change on retry: the checker chain deterministically
# re-derives the identical output from the identical pinned body, so
# retrying only adds load/latency without raising the success probability.
_DETERMINISTIC_RETRYABLE_REASON_CODES = frozenset({"spawn_failure", "signal", "capture_failure"})


def retry_matrix(*, backend: str, initial_session_ref: str | None, attempt: int, reason_code: str) -> RetryIntent | None:
    """Closed three-attempt matrix; no implicit fourth or fallback path.

    Issue #2165 P1-3: for `backend == "deterministic"` (the checker-pipeline
    child spawned by `run_root_review_pipeline.py`'s `run-checker-attempt`),
    the retryable set is `_DETERMINISTIC_RETRYABLE_REASON_CODES` (a closed
    allowlist), not the generic set below with timeout subtracted. Other
    backends keep the original retryable set unchanged (an LLM session hang
    is plausibly transient in a way a deterministic subprocess budget
    overrun is not).
    """
    retryable_reason_codes = {
        "spawn_failure",
        "timeout",
        "signal",
        "empty_output",
        "nonzero_exit",
        "malformed_output",
        "capture_failure",
        "artifact_validation_failure",
    }
    if backend == "deterministic":
        retryable_reason_codes = set(_DETERMINISTIC_RETRYABLE_REASON_CODES)
    if attempt >= MAX_ATTEMPTS or reason_code not in retryable_reason_codes:
        return None
    if attempt == 1 and initial_session_ref:
        return RetryIntent("same_session_resume", initial_session_ref)
    return RetryIntent("fresh_session_replacement", None)


# ---------------------------------------------------------------------------
# Backend command adapter (PR #2142 owner REQUEST_CHANGES P0-1)
# ---------------------------------------------------------------------------

# Issue #2161: the native Codex CLI executable backend ("codex") was
# retired from this transport (`native_codex_cli.disposition: retire`,
# `compatibility_preservation: not_required`) -- no replacement Codex
# adapter or compatibility shim was added.
_KNOWN_BACKENDS = frozenset({"claude", "deterministic", "fixture"})


def build_backend_command(*, backend: str, base_argv: list[str], session_ref: str | None) -> list[str]:
    """Materialize backend-specific same-session/fresh-session resume
    identity into real argv for one attempt.

    This is the single place a backend command adapter lives; callers (in
    particular ``run_reviewer_transport()``) must call this once per
    attempt with the attempt's *current* ``session_ref`` -- not reuse a
    single ``command`` value across retries -- so the resume/replacement
    intent ``retry_matrix()`` decides is actually reflected in what gets
    executed.

    - ``claude``: same-session resume passes ``--resume <session_ref>``;
      fresh-session replacement omits it (a new agent ID is assigned by the
      launched process, not synthesized here).
    - ``deterministic``: no session concept (idempotent checker pipeline);
      argv is returned unchanged regardless of ``session_ref``.
    - ``fixture``: test-only passthrough, unchanged.

    Issue #2161: the native Codex CLI executable backend (``codex``) has
    been removed from ``_KNOWN_BACKENDS`` and no longer has a branch here.
    """
    if backend not in _KNOWN_BACKENDS:
        raise ValueError(f"unknown_backend:{backend}")
    if not base_argv:
        raise ValueError("empty_base_argv")
    if backend == "claude":
        return [*base_argv, "--resume", session_ref] if session_ref else list(base_argv)
    return list(base_argv)


def build_compact_v2(
    *,
    verdict: str,
    summary: str,
    blockers: int,
    reviewed_body_sha256: str,
    attempt_id: str,
    artifact_relative: str,
    artifact_sha256: str,
    must_read: str = "",
) -> bytes:
    if (
        verdict not in {"approve", "needs-fix"}
        or blockers < 0
        or not _SHA.match(reviewed_body_sha256)
        or not _ATTEMPT.match(attempt_id)
        or not _SHA.match(artifact_sha256)
    ):
        raise ValueError("invalid_compact_v2_values")
    action = "proceed" if verdict == "approve" else "request_changes"
    if (verdict == "approve" and blockers != 0) or (verdict == "needs-fix" and blockers == 0):
        raise ValueError("compact_v2_cross_field_invalid")
    values = {
        "SCHEMA": SCHEMA_V2,
        "STATUS": "ok",
        "VERDICT": verdict,
        "SUMMARY": summary,
        "BLOCKERS": str(blockers),
        "NEXT_ACTION": action,
        "MUST_READ": must_read,
        "REVIEWED_BODY_SHA256": reviewed_body_sha256,
        "ATTEMPT_ID": attempt_id,
        "ARTIFACT": "compact_review_result_v2=" + artifact_relative,
        "ARTIFACT_SHA256": artifact_sha256,
    }
    return ("\n".join(f"{key}: {values[key]}" for key in V2_FIELDS) + "\n").encode("utf-8")


def validate_compact_v2(
    raw: bytes | str, *, issue_number: int | None = None, invocation_id: str | None = None, attempt: int | None = None
) -> dict[str, Any]:
    wire = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    result: dict[str, Any] = {
        "schema": "REVIEW_COMPACT_VALIDATION_RESULT_V2",
        "validation_status": "invalid",
        "normalized_payload": None,
        "violations": [],
        "input_sha256": sha256_prefixed(wire),
        "input_byte_count": len(wire),
    }
    try:
        text = wire.decode("utf-8", "strict")
    except UnicodeDecodeError:
        result["violations"].append({"code": "utf8_decode_error"})
        return result
    if not text.endswith("\n") or "\r" in text or len(wire) > 2048:
        result["violations"].append({"code": "wire_format_invalid"})
        return result
    lines = text[:-1].split("\n")
    if len(lines) != len(V2_FIELDS):
        result["violations"].append({"code": "line_count_invalid"})
        return result
    values: dict[str, str] = {}
    for expected, line in zip(V2_FIELDS, lines):
        if not line.startswith(expected + ": "):
            result["violations"].append({"code": "field_order_or_name_invalid", "field": expected})
            return result
        value = line[len(expected) + 2 :]
        if "\x00" in value or "\n" in value:
            result["violations"].append({"code": "control_character"})
            return result
        values[expected] = value
    artifact_prefix = "compact_review_result_v2="
    if (
        values["SCHEMA"] != SCHEMA_V2
        or values["STATUS"] != "ok"
        or values["VERDICT"] not in {"approve", "needs-fix"}
        or values["NEXT_ACTION"] != ("proceed" if values["VERDICT"] == "approve" else "request_changes")
    ):
        result["violations"].append({"code": "value_invalid"})
        return result
    if (
        not values["BLOCKERS"].isdigit()
        or (values["VERDICT"] == "approve") != (values["BLOCKERS"] == "0")
        or not _SHA.match(values["REVIEWED_BODY_SHA256"])
        or not _SHA.match(values["ARTIFACT_SHA256"])
        or not _ATTEMPT.match(values["ATTEMPT_ID"])
        or not values["ARTIFACT"].startswith(artifact_prefix)
    ):
        result["violations"].append({"code": "cross_field_invalid"})
        return result
    relative = values["ARTIFACT"][len(artifact_prefix) :]
    try:
        parts = _relative_parts(relative)
        if issue_number is not None and (not parts or parts[0] != str(issue_number)):
            raise ValueError("issue_mismatch")
        if invocation_id is not None and (len(parts) < 2 or parts[1] != invocation_id):
            raise ValueError("invocation_mismatch")
        if attempt is not None and (len(parts) < 3 or parts[2] != f"attempt-{attempt:03d}"):
            raise ValueError("attempt_mismatch")
    except ValueError as exc:
        result["violations"].append({"code": str(exc)})
        return result
    result.update(validation_status="valid", normalized_payload=values)
    return result


def secure_read_json(
    *, artifact_root: Path, artifact_relative: str, max_bytes: int = ARTIFACT_MAX_BYTES
) -> dict[str, Any]:
    """Open+read exact artifact bytes exactly once via dir-fd-anchored,
    component-wise ``O_DIRECTORY|O_NOFOLLOW`` traversal, then parse the SAME
    raw bytes with both SHA-256 and strict JSON.  No ``Path.resolve()`` ->
    ``stat()`` -> separate ``open()`` fallback is used anywhere in this path
    (AC6).  Callers layer schema/binding-specific checks on top of this
    generic, reusable primitive; this function performs no such checks
    itself.
    """
    try:
        root_fd, leaf_fd = _open_no_follow(artifact_root, artifact_relative)
        try:
            file_stat = os.fstat(leaf_fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > max_bytes:
                raise ValueError("non_regular_or_oversize_artifact")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(leaf_fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(leaf_fd)
            os.close(root_fd)
        if len(raw) > max_bytes:
            raise ValueError("raw_byte_oversize")
        payload = strict_json_loads(raw)
        return {"status": "valid", "raw_bytes": raw, "sha256": sha256_prefixed(raw), "payload": payload}
    except (OSError, ValueError, NotImplementedError, json.JSONDecodeError) as exc:
        return {"status": "integrity_failure", "reason_code": str(exc)}


def check_artifact_binding(
    payload: dict[str, Any],
    *,
    expected_repo: str,
    expected_issue: int,
    expected_body_sha256: str,
    expected_invocation_id: str,
    expected_attempt: int,
) -> str | None:
    """Canonical binding-field equality check on an ALREADY schema-verified
    ``payload`` dict (Issue #2242).  This is the SAME comparison
    :func:`verify_artifact` performs; it is factored out here so a caller
    that already holds a verified payload in memory (and therefore does not
    need to repeat :func:`verify_artifact`'s own open+read+hash I/O, e.g.
    ``run_root_review_pipeline.readback_persisted_artifact()``) can reuse
    the exact fail-closed decision instead of reimplementing a separate
    loose ``.get()`` comparison. Returns the fail-closed reason code
    (``"artifact_binding_mismatch"``) or ``None`` if every field matches.
    """
    # Issue #2242 OWNER adversarial review Blocker 4
    # (source-repo PR #2246 comment 5328161000):
    # `payload.get(key) != value` alone is INSECURE for `issue_number` /
    # `attempt` -- Python's `bool` is an `int` subclass, so a payload with
    # `issue_number: true` and `expected_issue=1` compares EQUAL
    # (`True != 1` is `False`). Exact `type(...) is int` checks reject a
    # bool-typed binding field outright, regardless of its truthy value.
    if type(payload.get("issue_number")) is not int or payload.get("issue_number") != expected_issue:
        return "artifact_binding_mismatch"
    if type(payload.get("attempt")) is not int or payload.get("attempt") != expected_attempt:
        return "artifact_binding_mismatch"
    string_fields = {
        "repository": expected_repo,
        "reviewed_body_sha256": expected_body_sha256,
        "invocation_id": expected_invocation_id,
    }
    if any(payload.get(key) != value for key, value in string_fields.items()):
        return "artifact_binding_mismatch"
    return None


def extract_binding_context(payload: Any) -> dict[str, Any] | None:
    """Extract + type-validate a V2 artifact's OWN binding-context fields
    (``repository`` / ``issue_number`` / ``invocation_id`` / ``attempt``) as
    a single canonical accessor (Issue #2242 AC7), so a consumer that
    already holds a schema-verified payload never maintains a second,
    independent field-map of this layout. Returns ``None`` if ``payload``
    is not a :data:`ARTIFACT_SCHEMA` object or any binding field is
    missing/mistyped/malformed.

    Issue #2242 OWNER adversarial review Blocker 4
    (source-repo PR #2246 comment 5328161000):
    the previous revision used ``isinstance(x, int)`` for ``issue_number`` /
    ``attempt``, which -- because Python's ``bool`` is an ``int`` subclass --
    silently accepted JSON ``true``/``false`` as a valid issue number or
    attempt. It also accepted an empty ``repository`` / ``invocation_id``
    string and applied no positive-value constraint. This revision uses
    exact ``type(x) is int`` checks (rejecting ``bool``), a ``>= 1``
    constraint on both integer fields, the canonical owner/repo format regex
    already defined by ``command_registry._OWNER_REPO_RE`` (reused, not
    duplicated), and the canonical invocation-id token format already
    defined by this module's own ``_ATTEMPT`` regex (the same format
    ``artifact_relative_path()`` already enforces on write).

    NOTE: this function is used ONLY to validate the SHAPE of an artifact's
    own embedded binding fields (schema-conformance gate). It is never used
    to derive "expected" values for a binding comparison against itself --
    see Issue #2242 Blocker 2 / ``run_root_review_pipeline.readback_persisted_artifact()``,
    which now requires the caller to supply independent expected binding
    values and delegates the actual comparison to :func:`verify_artifact`.
    """
    if not isinstance(payload, dict) or payload.get("schema") != ARTIFACT_SCHEMA:
        return None
    repository = payload.get("repository")
    issue_number = payload.get("issue_number")
    invocation_id = payload.get("invocation_id")
    attempt = payload.get("attempt")
    reviewed_body_sha256 = payload.get("reviewed_body_sha256")
    if (
        not isinstance(repository, str)
        or not _CANONICAL_OWNER_REPO_RE.match(repository)
        or type(issue_number) is not int
        or issue_number < 1
        or not isinstance(invocation_id, str)
        or not _ATTEMPT.match(invocation_id)
        or type(attempt) is not int
        or attempt < 1
        or not isinstance(reviewed_body_sha256, str)
        or not _SHA.match(reviewed_body_sha256)
    ):
        return None
    return {
        "repository": repository,
        "issue_number": issue_number,
        "invocation_id": invocation_id,
        "attempt": attempt,
    }


def validate_semantic_result_schema(semantic_result: Any) -> str | None:
    """Validate ``semantic_result`` against the FULL ``REVIEW_ISSUE_RESULT_V1``
    jsonschema (Issue #2242 OWNER adversarial review Blocker 4).

    Reuses the canonical jsonschema-based validator already defined by
    ``review-issue/scripts/check_issue_contract.py``
    (``_validate_review_issue_result_payload()``) instead of reimplementing a
    second, weaker structural check (the prior revision only checked
    ``verdict``/``blocking_issues`` presence via
    ``_semantic_verdict_and_count()``). Returns a stable violation code
    string on failure, or ``None`` when ``semantic_result`` validates
    cleanly.
    """
    if not isinstance(semantic_result, dict):
        return "semantic_result_schema_invalid"
    try:
        _check_issue_contract._validate_review_issue_result_payload(semantic_result)
    except Exception:
        return "semantic_result_schema_invalid"
    return None


def semantic_verdict_and_count(semantic_result: Any) -> tuple[str, int]:
    """Public wrapper around ``_semantic_verdict_and_count()`` for external
    consumers (e.g.
    ``run_root_review_pipeline.readback_persisted_artifact()``) that must
    not reimplement ``semantic_result``'s internal key layout (Issue #2242
    AC7/design point 2)."""
    return _semantic_verdict_and_count(semantic_result)


def verify_artifact(
    *,
    artifact_root: Path,
    artifact_relative: str,
    expected_repo: str,
    expected_issue: int,
    expected_body_sha256: str,
    expected_invocation_id: str,
    expected_attempt: int,
    expected_sha256: str,
) -> dict[str, Any]:
    """Verify exact artifact bytes once.  Any inability is integrity failure."""
    read = secure_read_json(artifact_root=artifact_root, artifact_relative=artifact_relative)
    if read["status"] != "valid":
        return read
    raw = read["raw_bytes"]
    payload = read["payload"]
    if read["sha256"] != expected_sha256:
        return {"status": "integrity_failure", "reason_code": "raw_byte_hash_mismatch"}
    if not isinstance(payload, dict) or payload.get("schema") != ARTIFACT_SCHEMA:
        return {"status": "integrity_failure", "reason_code": "schema_mismatch"}
    # Issue #2242 OWNER adversarial review Blocker 4: a schema-shape gate on
    # the artifact's OWN binding fields (exact int types, positive values,
    # canonical owner/repo / invocation-id / body-sha256 formats) BEFORE the
    # binding-value comparison below, so a malformed/mistyped artifact
    # (e.g. `issue_number: true`, `repository: ""`) is rejected even if a
    # value-level comparison could otherwise be fooled.
    if extract_binding_context(payload) is None:
        return {"status": "integrity_failure", "reason_code": "schema_mismatch"}
    binding_violation = check_artifact_binding(
        payload,
        expected_repo=expected_repo,
        expected_issue=expected_issue,
        expected_body_sha256=expected_body_sha256,
        expected_invocation_id=expected_invocation_id,
        expected_attempt=expected_attempt,
    )
    if binding_violation is not None:
        return {"status": "integrity_failure", "reason_code": binding_violation}
    return {"status": "valid", "raw_bytes": raw, "payload": payload}


def write_semantic_artifact(
    *,
    artifact_root: Path,
    issue_number: int,
    repo: str,
    invocation_id: str,
    attempt: int,
    reviewed_body_sha256: str,
    semantic_result: dict[str, Any],
) -> tuple[str, str]:
    """Persist the full semantic review result losslessly (PR #2142 owner
    REQUEST_CHANGES P0-3): ``semantic_result`` MUST be the complete
    ``REVIEW_ISSUE_RESULT_V1``-shaped payload (``findings[]`` /
    ``checker_evidence`` / ``structured_blockers`` / etc., not merely
    ``{"verdict": ..., "blocking_issues": ...}``), so a rewrite lane can
    recover full provenance from the verified V2 artifact alone. This
    function itself is schema-agnostic; it is the caller's responsibility
    to pass the full payload.
    """
    relative = artifact_relative_path(issue_number, invocation_id, attempt)
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "repository": repo,
        "issue_number": issue_number,
        "reviewed_body_sha256": reviewed_body_sha256,
        "invocation_id": invocation_id,
        "attempt": attempt,
        "semantic_result": semantic_result,
    }
    raw = canonical_json_bytes(payload)
    _atomic_create(artifact_root, relative, raw)
    return relative, sha256_prefixed(raw)


# ---------------------------------------------------------------------------
# Wire <-> artifact semantic cross-binding (PR #2142 owner REQUEST_CHANGES P0-2)
# ---------------------------------------------------------------------------


def _semantic_verdict_and_count(semantic_result: Any) -> tuple[str, int]:
    if not isinstance(semantic_result, dict):
        raise ValueError("semantic_result_missing")
    verdict = semantic_result.get("verdict")
    blocking_issues = semantic_result.get("blocking_issues")
    if verdict not in {"approve", "needs-fix"} or not isinstance(blocking_issues, list):
        raise ValueError("semantic_result_invalid")
    count = len(blocking_issues)
    if (verdict == "approve" and count != 0) or (verdict == "needs-fix" and count == 0):
        raise ValueError("semantic_result_cross_field_invalid")
    return verdict, count


def project_compact_v2_from_artifact(
    payload: dict[str, Any], *, attempt_id: str, artifact_relative: str, artifact_sha256: str
) -> bytes:
    """Re-derive the canonical V2 wire *purely* from a verified artifact's
    ``semantic_result`` (never from a separately relayed wire string), so a
    caller can prove that wire still reflects the artifact's actual verdict.
    """
    verdict, count = _semantic_verdict_and_count(payload.get("semantic_result"))
    reviewed_body_sha256 = payload.get("reviewed_body_sha256")
    if not isinstance(reviewed_body_sha256, str):
        raise ValueError("reviewed_body_sha256_missing")
    summary = "contract ready" if verdict == "approve" else f"{count} blocker(s)"
    return build_compact_v2(
        verdict=verdict,
        summary=summary,
        blockers=count,
        reviewed_body_sha256=reviewed_body_sha256,
        attempt_id=attempt_id,
        artifact_relative=artifact_relative,
        artifact_sha256=artifact_sha256,
    )


def verify_wire_matches_artifact(
    *,
    wire: bytes | str,
    verified_artifact: dict[str, Any],
    artifact_relative: str,
    artifact_sha256: str,
) -> dict[str, Any]:
    """Cross-check a compact V2 wire against the SAME verified artifact's
    ``semantic_result`` (PR #2142 owner REQUEST_CHANGES P0-2).

    ``verify_artifact()`` alone (hash/schema/repository/issue/body-SHA/
    invocation/attempt binding on the artifact bytes) does not prove that a
    *separately relayed* wire string (e.g. an agent's stdout, forwarded by
    an orchestrator without re-deriving it from the artifact) still reflects
    that artifact's verdict/blocker count: a relay could keep
    ``ARTIFACT``/``ARTIFACT_SHA256`` pointing at a legitimate,
    hash-verified artifact while self-consistently rewriting
    ``VERDICT``/``BLOCKERS``/``NEXT_ACTION`` to something the artifact does
    not actually contain. This function closes that gap by re-deriving the
    canonical wire from the artifact and comparing byte-for-byte against
    the wire under test, treating the artifact (not the wire) as ground
    truth.
    """
    payload = verified_artifact.get("payload")
    if not isinstance(payload, dict):
        return {"status": "integrity_failure", "reason_code": "artifact_payload_missing"}
    invocation_id = payload.get("invocation_id")
    if not isinstance(invocation_id, str):
        return {"status": "integrity_failure", "reason_code": "artifact_invocation_id_missing"}
    try:
        expected_wire = project_compact_v2_from_artifact(
            payload, attempt_id=invocation_id, artifact_relative=artifact_relative, artifact_sha256=artifact_sha256
        )
    except ValueError as exc:
        return {"status": "integrity_failure", "reason_code": f"artifact_projection_failed:{exc}"}
    actual = wire if isinstance(wire, bytes) else wire.encode("utf-8")
    if actual != expected_wire:
        return {"status": "integrity_failure", "reason_code": "wire_artifact_semantic_mismatch"}
    return {"status": "valid"}


def _attempt_result(**kwargs: Any) -> dict[str, Any]:
    # Issue #2165 P1-4: `timeout_phase` is an ADDITIVE, always-present
    # (nullable) field on every attempt result -- not a new closed-domain
    # `reason_code` value -- so a `reason_code: "timeout"` attempt can carry
    # WHICH layer timed out (`reviewer_transport_wait`,
    # `baseline_vc_preflight_aggregate`, `check_issue_contract`,
    # `merge_readiness`, etc.) without expanding the reason_code enum a
    # consumer must branch on.
    return {
        "schema": ATTEMPT_SCHEMA,
        "transport_status": "environment_failure",
        "semantic_verdict": None,
        "timeout_phase": None,
        **kwargs,
    }


def run_reviewer_transport(
    *,
    base_argv: list[str],
    command_id: str,
    argv_template_id: str,
    backend: str,
    issue_number: int,
    repo: str,
    reviewed_body_sha256: str,
    artifact_root: Path,
    invocation_id: str | None = None,
    session_ref: str | None = None,
    per_attempt_deadline: int = PER_ATTEMPT_DEADLINE_SECONDS,
    total_deadline: int = TOTAL_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Run a reviewer child with process-group reaping and closed retries.

    ``base_argv`` is the backend-invariant portion of the command; the
    actual per-attempt command is rebuilt every attempt via
    ``build_backend_command()`` using that attempt's *current*
    ``active_session`` (PR #2142 owner REQUEST_CHANGES P0-1), so the
    resume/replacement identity ``retry_matrix()`` decides is actually
    reflected in the spawned argv instead of reusing one fixed ``command``
    across every attempt.
    """
    if not base_argv or issue_number <= 0 or not _SHA.match(reviewed_body_sha256):
        raise ValueError("invalid_transport_request")
    invocation_id = invocation_id or generate_invocation_id()
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    active_session = session_ref
    retry_intent_kind = "initial"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        elapsed = time.monotonic() - started
        if elapsed >= total_deadline:
            break
        # Issue #2165 P1-1 marge condition 8: never spawn a RETRY (attempt
        # 2/3) once the time remaining before `total_deadline` drops below
        # `MIN_RETRY_ATTEMPT_BUDGET_FRACTION * per_attempt_deadline` -- an
        # attempt that cannot possibly complete spawn + minimal work +
        # cleanup in the time left would just burn the remaining budget
        # without a realistic chance of succeeding. The FIRST attempt is
        # exempt (it always gets its full per-attempt budget from a fresh
        # `total_deadline` clock).
        if attempt > 1 and not has_sufficient_retry_attempt_budget(
            elapsed_seconds=elapsed,
            total_deadline_seconds=total_deadline,
            per_attempt_deadline_seconds=per_attempt_deadline,
            # `min_fraction` is read from the MODULE global at call time
            # (not the function's own default parameter value, which is
            # bound once at def-time) so tests that monkeypatch
            # `MIN_RETRY_ATTEMPT_BUDGET_FRACTION` at the module level (e.g.
            # `test_reviewer_transport_deadline_matches_vc_worst_case.py`)
            # still take effect here.
            min_fraction=MIN_RETRY_ATTEMPT_BUDGET_FRACTION,
        ):
            break
        intent = retry_intent_kind
        command = build_backend_command(backend=backend, base_argv=base_argv, session_ref=active_session)
        manifest_relative = attempt_relative_dir(issue_number, invocation_id, attempt) + "/attempt_manifest.json"
        _write_json_once(
            artifact_root,
            manifest_relative,
            _make_manifest(
                issue_number=issue_number,
                repo=repo,
                invocation_id=invocation_id,
                attempt=attempt,
                backend=backend,
                session_ref=active_session,
                retry_intent=intent,
            ),
        )
        common = {
            "invocation_id": invocation_id,
            "attempt": attempt,
            "backend": backend,
            "session_ref": active_session,
            "command_id": command_id,
            "argv_template_id": argv_template_id,
            "rendered_argv_sha256": sha256_prefixed("\0".join(command).encode()),
            "pid": None,
            "process_group": None,
            "exit_code": None,
            "signal": None,
            "timeout": False,
            "descendants_reaped": None,
            "stdout_length": 0,
            "stdout_sha256": None,
            "stderr_length": 0,
            "stderr_sha256": None,
            "stdout_prefix": "<redacted:0-bytes>",
            "stdout_suffix": "<redacted:0-bytes>",
            "stderr_prefix": "<redacted:0-bytes>",
            "stderr_suffix": "<redacted:0-bytes>",
        }
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            result = _attempt_result(
                **common, failure_phase="spawn", reason_code="spawn_failure", spawn_error=type(exc).__name__
            )
            result_relative = attempt_relative_dir(issue_number, invocation_id, attempt) + "/attempt_result.json"
            _write_json_once(artifact_root, result_relative, result)
            results.append(result)
            retry = retry_matrix(
                backend=backend, initial_session_ref=session_ref, attempt=attempt, reason_code=result["reason_code"]
            )
            if retry is None:
                break
            active_session = retry.session_ref
            retry_intent_kind = retry.kind
            continue
        common.update(pid=process.pid, process_group=process.pid)
        stdout: dict[str, Any] = {}
        stderr: dict[str, Any] = {}
        # PR #2142 owner REQUEST_CHANGES P1-1: daemon=True so an escaped
        # grandchild that keeps the inherited stdout/stderr pipe write-end
        # open cannot force this function to block past the bounded join
        # below -- Python cannot safely cancel a thread blocked in a
        # blocking `read()` syscall (closing the fd from another thread
        # while a read is in flight is platform-dependent and unreliable,
        # not a real cancellation), so a daemon thread that is still alive
        # after its grace period is simply left running in the background
        # (its target bucket stays unpopulated) while this function
        # proceeds and classifies the attempt from whatever was captured.
        threads = [
            threading.Thread(target=_bounded_reader, args=(process.stdout, STDOUT_CAP, stdout), daemon=True),
            threading.Thread(target=_bounded_reader, args=(process.stderr, STDERR_CAP, stderr), daemon=True),
        ]
        [thread.start() for thread in threads]
        timed_out = False
        try:
            process.wait(timeout=min(per_attempt_deadline, max(1, total_deadline - (time.monotonic() - started))))
        except subprocess.TimeoutExpired:
            timed_out = True
        # PR #2142 owner REQUEST_CHANGES P1-1: a hard total-deadline check
        # AFTER wait() returns, independent of whether wait() itself timed
        # out.  Without this, `max(1, total_deadline - elapsed)` could grant
        # up to ~1 extra second past `total_deadline`, and a result that
        # only becomes available past the deadline was previously accepted
        # with no re-check (late-result rejection was not actually
        # enforced).
        if not timed_out and time.monotonic() - started > total_deadline:
            timed_out = True
        reaped: bool | None = None
        if timed_out:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            reaped = _confirm_process_group_reaped(process.pid)
        # PR #2142 owner REQUEST_CHANGES P1-1: bounded join.  An escaped
        # grandchild (one that called setsid() and left the process group
        # `killpg()` targets) can hold the inherited stdout/stderr pipe FD
        # open even after the direct child has exited, so an unbounded
        # `thread.join()` can block forever on that FD's EOF.  A join
        # timeout, plus a subsequent stream close to unblock a still-live
        # reader thread, keeps this bounded.
        reader_timed_out = False
        for thread in threads:
            thread.join(timeout=READER_JOIN_GRACE_SECONDS)
            if thread.is_alive():
                reader_timed_out = True
        common.update(
            timeout=timed_out,
            descendants_reaped=reaped,
            exit_code=process.returncode,
            signal=(-process.returncode if process.returncode and process.returncode < 0 else None),
            stdout_length=stdout.get("length", 0),
            stdout_sha256=stdout.get("sha256"),
            stderr_length=stderr.get("length", 0),
            stderr_sha256=stderr.get("sha256"),
            stdout_prefix=_redact_bytes(stdout.get("prefix", b"")),
            stdout_suffix=_redact_bytes(stdout.get("suffix", b"")),
            stderr_prefix=_redact_bytes(stderr.get("prefix", b"")),
            stderr_suffix=_redact_bytes(stderr.get("suffix", b"")),
        )
        if timed_out:
            reason_code = "timeout" if not reader_timed_out else "capture_failure"
            result = _attempt_result(
                **common,
                failure_phase="execution",
                reason_code=reason_code,
                timeout_phase=("reviewer_transport_wait" if reason_code == "timeout" else None),
            )
        elif process.returncode != 0:
            # Issue #2165 P1-4 (OWNER 2026-08-15 REQUEST_CHANGES): a
            # structured `{"error_code": "timeout", "timeout_phase": ...}`
            # the child itself detected (e.g.
            # `run_root_review_pipeline.py _cmd_run_checker_attempt()`
            # catching an inner checker's `subprocess.TimeoutExpired`, or
            # `contract_readiness_check.py` reporting its own typed
            # `status: "runtime_error"` baseline_vc_preflight aggregate
            # timeout) is reclassified to the SAME `timeout` reason_code
            # this branch's sibling (`timed_out` above) already uses -- NOT
            # a separate `inner_timeout` value -- so the reason_code domain
            # stays closed (`retry_matrix()`'s deterministic-backend
            # allowlist only has to reason about one `timeout` value) and
            # does not compete with Issue #2040's ongoing timeout_phase
            # taxonomy work. `timeout_phase` (additive, see
            # `_attempt_result()`) records WHICH layer reported it. A parse
            # failure (non-JSON stderr, unrelated error shape, or JSON
            # without `error_code == "timeout"`) intentionally falls back
            # to `nonzero_exit` unchanged -- this is additive
            # classification, not a new failure mode.
            child_timeout_phase: str | None = None
            if not common["signal"]:
                try:
                    stderr_payload = strict_json_loads(stderr.get("prefix", b"") or b"")
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    stderr_payload = None
                if isinstance(stderr_payload, dict) and stderr_payload.get("error_code") == "timeout":
                    phase = stderr_payload.get("timeout_phase")
                    child_timeout_phase = phase if isinstance(phase, str) and phase else "unspecified"
            if common["signal"]:
                execution_reason_code = "signal"
            elif child_timeout_phase is not None:
                execution_reason_code = "timeout"
            else:
                execution_reason_code = "nonzero_exit"
            result = _attempt_result(
                **common,
                failure_phase="execution",
                reason_code=execution_reason_code,
                timeout_phase=child_timeout_phase,
            )
        elif not stdout.get("length"):
            result = _attempt_result(**common, failure_phase="capture", reason_code="empty_output")
        elif stdout["length"] > STDOUT_CAP:
            result = _attempt_result(**common, failure_phase="capture", reason_code="capture_failure")
        else:
            # The child returns structured review JSON.  It never authors the
            # compact wire or artifact: this parent is the V2 producer.
            try:
                semantic = strict_json_loads(stdout.get("prefix", b""))
                verdict, count = _semantic_verdict_and_count(semantic)
                relative, digest = write_semantic_artifact(
                    artifact_root=artifact_root,
                    issue_number=issue_number,
                    repo=repo,
                    invocation_id=invocation_id,
                    attempt=attempt,
                    reviewed_body_sha256=reviewed_body_sha256,
                    semantic_result=semantic,
                )
                wire = build_compact_v2(
                    verdict=verdict,
                    summary=("contract ready" if verdict == "approve" else f"{count} blocker(s)"),
                    blockers=count,
                    reviewed_body_sha256=reviewed_body_sha256,
                    attempt_id=invocation_id,
                    artifact_relative=relative,
                    artifact_sha256=digest,
                )
                compact = validate_compact_v2(
                    wire, issue_number=issue_number, invocation_id=invocation_id, attempt=attempt
                )
                if compact["validation_status"] != "valid":
                    raise ValueError("parent_compact_validation_failure")
                fields = compact["normalized_payload"]
                verified = verify_artifact(
                    artifact_root=artifact_root,
                    artifact_relative=relative,
                    expected_repo=repo,
                    expected_issue=issue_number,
                    expected_body_sha256=reviewed_body_sha256,
                    expected_invocation_id=invocation_id,
                    expected_attempt=attempt,
                    expected_sha256=digest,
                )
                if verified["status"] != "valid":
                    result = _attempt_result(
                        **common,
                        failure_phase="artifact_validation",
                        reason_code="integrity_failure",
                        artifact_validation=verified,
                    )
                else:
                    # PR #2142 owner REQUEST_CHANGES P0-2: even though this
                    # code path derives `wire` and `verified` from the SAME
                    # local `semantic`/`verdict`/`count`, self-check the
                    # projection here too so any future refactor that lets
                    # them diverge fails closed instead of silently
                    # regressing the binding this function exists to
                    # provide.
                    cross = verify_wire_matches_artifact(
                        wire=wire, verified_artifact=verified, artifact_relative=relative, artifact_sha256=digest
                    )
                    if cross["status"] != "valid":
                        result = _attempt_result(
                            **common,
                            failure_phase="artifact_validation",
                            reason_code="integrity_failure",
                            artifact_validation=cross,
                        )
                    else:
                        result = {
                            "schema": ATTEMPT_SCHEMA,
                            "transport_status": "ok",
                            "semantic_verdict": verdict,
                            **common,
                            "failure_phase": None,
                            "reason_code": None,
                            "compact": fields,
                        }
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                result = _attempt_result(
                    **common,
                    failure_phase="compact_validation",
                    reason_code="malformed_output",
                    compact_error=type(exc).__name__,
                )
        result_relative = attempt_relative_dir(issue_number, invocation_id, attempt) + "/attempt_result.json"
        _write_json_once(artifact_root, result_relative, result)
        results.append(result)
        if result["transport_status"] == "ok":
            return {
                "schema": "REVIEWER_TRANSPORT_RESULT_V1",
                "transport_status": "ok",
                "semantic_verdict": result["semantic_verdict"],
                "invocation_id": invocation_id,
                # Stage 1b-3: additive binding fields so a consumer can reject a stale / foreign /
                # fixture result instead of trusting schema + verdict alone.
                "backend": backend,
                "issue_number": issue_number,
                "repository": repo,
                "reviewed_body_sha256": reviewed_body_sha256,
                "attempts": results,
            }
        retry = retry_matrix(
            backend=backend, initial_session_ref=session_ref, attempt=attempt, reason_code=result["reason_code"]
        )
        if retry is None:
            break
        active_session = retry.session_ref
        retry_intent_kind = retry.kind
    return {
        "schema": "REVIEWER_TRANSPORT_RESULT_V1",
        "transport_status": "environment_failure",
        "semantic_verdict": None,
        "invocation_id": invocation_id,
        "attempts": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run/validate reviewer transport V2")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--invocation-id")
    parser.add_argument("--attempt", type=int)
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read()
    if not args.validate:
        parser.error("only --validate is a CLI surface; execution is parent API only")
    value = validate_compact_v2(
        raw, issue_number=args.issue_number, invocation_id=args.invocation_id, attempt=args.attempt
    )
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    return 0 if value["validation_status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())

