#!/usr/bin/env python3
"""
Local per-repository SQLite history store for VC command-level execution
duration samples (Issue #2254).

This module is INTENTIONALLY separate from `baseline_vc_preflight.py`
(Issue #2254 Current Validated Scope): it owns the SQLite persistence
layer, the `nearest_rank_v1` percentile estimator, the right-censored
timeout backoff calculation, and the immutable `history_snapshot/v1`
producer. `baseline_vc_preflight.py` only WIRES this module's output into
`compute_command_timeout_budget()` / `compute_canonical_vc_plan()` as an
additive `history_estimate` source -- it never talks to SQLite directly.

Design invariants (Issue #2254 Outcome / In Scope):

  * ADVISORY, RAISE-ONLY: this module never lowers an existing
    `static_policy` / `static_fallback` timeout. It only ever proposes a
    CANDIDATE value; `baseline_vc_preflight.py`'s
    `compute_command_timeout_budget()` is the sole place that applies the
    `max(static_base, history_candidate, timeout_backoff_floor)`
    raise-only precedence.
  * IMMUTABLE SNAPSHOT: the SQLite store itself is mutable (concurrent
    writers append samples continuously), but `build_history_snapshot()`
    is the SINGLE root-owned read of that store for one canonical-VC-plan
    computation. Once a snapshot dict has been built, it is a plain,
    side-effect-free, JSON-serializable value: re-deriving a plan from the
    SAME snapshot object always yields the SAME `plan_digest`, regardless
    of what the store looks like by the time that digest is later
    recomputed (Issue #2254 AC1).
  * NON-BLOCKING DEGRADATION: every store-access failure mode (missing
    file, lock contention, corruption, unknown schema, invalid rows,
    fingerprint mismatch, staleness) degrades to an EMPTY snapshot
    (`store_status != "ok"`, `records: {}`) rather than raising or
    blocking. A caller that receives a degraded snapshot simply falls
    back to `static_policy` / `static_fallback` for every command (Issue
    #2254 AC8).
  * IDENTITY SEPARATION (Issue #2254 AC2): `command_group_key` (WHAT is
    being run, for HISTORY GROUPING purposes only), `environment_fingerprint`
    (in WHICH kind of environment), and `execution_id` (WHICH single
    subprocess launch) are three independent concepts. None of them reuses
    `baseline_vc_preflight.compute_execution_key_hash()` (an unrelated,
    finer-grained dedup-replay key that additionally binds env delta /
    resolved timeout / state epoch -- mixing that into history GROUPING
    would make textually-identical commands stop sharing history the
    moment their resolved timeout changed, which is exactly the value
    history is trying to compute).

Out of scope (Issue #2254 Out of Scope, mirrored here for locality):
  * CI cross-run persistence / `ci_runtime_baseline_v1` schema reuse (see
    `.claude/skills/issue-contract-review/references/vc-preflight.md` for
    the scope-boundary rationale -- Issue #2254 AC10).
  * Any cryptographic (HMAC / signature) tamper-proofing. Health checks
    here are plain type/range/schema-version/duplicate-key checks against
    a same-user-privilege local DB, not a security boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

# ---------------------------------------------------------------------------
# Fixed contract constants (Issue #2254 AC3 nearest_rank_v1)
# ---------------------------------------------------------------------------

HISTORY_STORE_SCHEMA_VERSION = 1
HISTORY_SNAPSHOT_SCHEMA = "history_snapshot/v1"
WRITER_VERSION = "v1"

# `nearest_rank_v1` fixed contract (Issue #2254 AC3 -- do NOT change these
# without an explicit Stop Condition escalation; the Issue body pins them).
HISTORY_TTL_DAYS = 30
HISTORY_WINDOW_MAX_SAMPLES = 50
HISTORY_MIN_SUCCESS_SAMPLES = 5
HISTORY_SAFETY_MARGIN_MULTIPLIER = 1.5

# SQLite busy_timeout bound (Issue #2254 In Scope: "busy timeout 0.25〜1秒").
DEFAULT_BUSY_TIMEOUT_SECONDS = 0.5
MIN_BUSY_TIMEOUT_SECONDS = 0.25
MAX_BUSY_TIMEOUT_SECONDS = 1.0

SAMPLE_STATUSES = {"success", "failed", "timed_out", "cancelled"}

_ENV_STORE_PATH_OVERRIDE = "VC_RUNTIME_HISTORY_STORE_PATH"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    command_group_key TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    applied_timeout_ms INTEGER,
    command_hash TEXT NOT NULL,
    writer_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_group_env_time
    ON samples(command_group_key, environment_fingerprint, observed_at_utc);
"""


# ---------------------------------------------------------------------------
# Identity: command_group_key / environment_fingerprint / execution_id
# ---------------------------------------------------------------------------


def compute_command_group_key(command: str, cwd: str, *, repo_root: Optional[str] = None) -> str:
    """Grouping key for HISTORY purposes only (Issue #2254 AC2).

    Derived from the RAW command TEXT (Issue #2254 fix_delta P2 OWNER
    warning source-repo PR 2382 review comment 5458281756:
    `shlex.split()` is POSIX-mode QUOTE-STRIPPING, not a byte-for-byte
    preserving normalization -- see Python's `shlex` docs -- so hashing a
    split/rejoined argv here would silently CONFLATE two commands that
    differ only in shell quoting, contradicting this function's own "never
    normalized" contract; hashing the raw text avoids that entirely), the
    repo-relative cwd (`repo_root`, when given -- Issue #2254 fix_delta P0
    blocker 2: this is what lets history samples recorded from one
    worktree of a repository be shared by every OTHER worktree of the SAME
    repository, instead of fragmenting into one bucket per worktree's
    absolute path), and the command "family" (argv[0], still derived via
    `shlex.split` purely for FAMILY EXTRACTION -- a best-effort
    classification label, not an identity-preserving transform of the
    grouping key itself). Deliberately does NOT include
    `applied_timeout_ms` / runner_env_delta / state_epoch -- changing the
    resolved timeout for a command must NOT change which history bucket it
    reads from (that would make the raise-only feedback loop unable to
    ever converge: a bigger timeout would look up a different, empty,
    bucket).
    """
    stripped_command = command.strip()
    try:
        argv = shlex.split(stripped_command)
        family = argv[0] if argv else ""
    except ValueError:
        # Unbalanced quotes etc: family extraction only, never raises (it
        # is only a GROUPING key, not a security check).
        parts = stripped_command.split()
        family = parts[0] if parts else ""
    resolved_cwd = os.path.realpath(cwd or ".")
    if repo_root:
        try:
            rel_cwd = os.path.relpath(resolved_cwd, os.path.realpath(repo_root))
        except ValueError:
            rel_cwd = resolved_cwd
    else:
        rel_cwd = resolved_cwd
    payload = json.dumps(
        {"command_text": stripped_command, "cwd": rel_cwd, "family": family},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_environment_fingerprint(
    command_family: str,
    *,
    lock_digest: str = "",
    tool_version: str = "",
    platform_tag: Optional[str] = None,
    arch_tag: Optional[str] = None,
    runner_class: Optional[str] = None,
) -> str:
    """Sample-compatibility fingerprint (Issue #2254 AC2 / In Scope).

    Deliberately excludes ALL volatile values (full environment variables,
    tokens/credentials, temp paths, `GITHUB_RUN_ID`, PID, timestamps).
    Only platform/arch/runner-class/command-family and a caller-supplied
    `lock_digest` / `tool_version` (the two things that actually determine
    whether a past duration sample is comparable to a future run of the
    SAME command family) are included.
    """
    resolved_platform = platform_tag if platform_tag is not None else sys.platform
    if arch_tag is not None:
        resolved_arch = arch_tag
    else:
        try:
            resolved_arch = os.uname().machine  # type: ignore[attr-defined]
        except AttributeError:
            resolved_arch = "unknown"
    resolved_runner_class = runner_class
    if resolved_runner_class is None:
        resolved_runner_class = "ci" if os.environ.get("CI") else "local"
    payload = json.dumps(
        {
            "command_family": command_family,
            "platform": resolved_platform,
            "arch": resolved_arch,
            "runner_class": resolved_runner_class,
            "lock_digest": lock_digest,
            "tool_version": tool_version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_execution_id() -> str:
    """One value per ACTUAL subprocess launch (Issue #2254 AC6: 'one
    launch, one sample'). Callers must generate exactly ONE of these per
    real `subprocess`/supervisor launch and reuse it for every AC/
    occurrence that dedup-replays that SAME launch's result, never
    generating a fresh one for a replay."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Store path resolution
# ---------------------------------------------------------------------------


def default_store_path(
    git_common_dir: str, *, xdg_state_home: Optional[str] = None
) -> Path:
    """`$XDG_STATE_HOME/agent-platform/vc-runtime-history/<sha256(realpath(git-common-dir))>.sqlite3`
    (Issue #2254 In Scope). `XDG_STATE_HOME` unset -> `~/.local/state`.
    Explicitly OUTSIDE the repo tree / Allowed Paths guard scope -- this is
    runtime state, never repo-tracked."""
    base = (
        xdg_state_home
        or os.environ.get("XDG_STATE_HOME")
        or str(Path.home() / ".local" / "state")
    )
    repo_key = hashlib.sha256(os.path.realpath(git_common_dir).encode("utf-8")).hexdigest()
    return Path(base) / "agent-platform" / "vc-runtime-history" / f"{repo_key}.sqlite3"


def resolve_store_path(git_common_dir: str) -> Path:
    """Production resolver: honors `VC_RUNTIME_HISTORY_STORE_PATH` (test-only
    override; production callers never set this) before falling back to
    `default_store_path()`."""
    override = os.environ.get(_ENV_STORE_PATH_OVERRIDE)
    if override:
        return Path(override)
    return default_store_path(git_common_dir)


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------


def _clamp_busy_timeout(busy_timeout_seconds: float) -> float:
    return max(MIN_BUSY_TIMEOUT_SECONDS, min(MAX_BUSY_TIMEOUT_SECONDS, busy_timeout_seconds))


def _connect(store_path: Path, *, busy_timeout_seconds: float) -> sqlite3.Connection:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(store_path), timeout=_clamp_busy_timeout(busy_timeout_seconds), isolation_level=None
    )
    conn.execute(f"PRAGMA busy_timeout = {int(_clamp_busy_timeout(busy_timeout_seconds) * 1000)}")
    # Explicit rollback journal mode (the SQLite default) -- Issue #2254 In
    # Scope pins this deliberately (no WAL) to keep the on-disk store a
    # single ordinary file with no companion -wal/-shm files to reason
    # about across processes.
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> str:
    """Returns 'ok' or 'unknown_schema'. Never raises for a schema-version
    mismatch (that is a degrade-to-fallback signal, not a fatal error).

    Issue #2254 fix_delta P1 (OWNER REQUEST_CHANGES
    source-repo PR 2382 review comment 5458281756
    blocker 6, AC7): the version row is seeded with `INSERT OR IGNORE`
    (never a plain `INSERT`), then unconditionally RE-READ. Two independent
    PROCESSES racing on a genuinely fresh (never-before-written) store can
    both observe "no version row yet" and both attempt to seed it; a plain
    `INSERT` would raise `sqlite3.IntegrityError` (a `sqlite3.DatabaseError`
    subclass) for the loser, which `record_sample()` maps to
    `store_status: "corrupt"` -- silently dropping that process's sample
    even though the store itself is perfectly healthy. `INSERT OR IGNORE`
    makes the loser's seed attempt a no-op instead of an error, and the
    subsequent `SELECT` always observes SOME winner's row, so both
    processes agree on `existing_version` and proceed to their own
    `BEGIN IMMEDIATE` sample insert normally."""
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(HISTORY_STORE_SCHEMA_VERSION),),
    )
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    if row is None:
        # Unreachable in practice (the INSERT OR IGNORE above guarantees a
        # row exists once this statement returns) -- kept as a defensive,
        # never-raising fallback rather than an assertion.
        return "unknown_schema"
    try:
        existing_version = int(row[0])
    except (TypeError, ValueError):
        return "unknown_schema"
    if existing_version != HISTORY_STORE_SCHEMA_VERSION:
        return "unknown_schema"
    return "ok"


# ---------------------------------------------------------------------------
# Writer: record_sample (Issue #2254 AC6 / AC7)
# ---------------------------------------------------------------------------


def record_sample(
    store_path: Path,
    *,
    execution_id: str,
    command_group_key: str,
    environment_fingerprint: str,
    status: str,
    command_hash: str,
    duration_ms: Optional[int] = None,
    applied_timeout_ms: Optional[int] = None,
    observed_at_utc: Optional[str] = None,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    writer_version: str = WRITER_VERSION,
) -> Dict[str, Any]:
    """Insert exactly one sample row, keyed by `execution_id` (the DB
    `sample_id` primary key IS the `execution_id` -- Issue #2254 AC6: a
    second `record_sample()` call for the SAME `execution_id` is a
    no-op/duplicate-reject via `INSERT OR IGNORE`, never a second row).

    NEVER raises. NEVER blocks beyond `busy_timeout_seconds`. Returns
    `{"recorded": bool, "store_status": "ok"|"locked"|"corrupt"|"invalid_row"|"error", "reason": str|None}`.
    """
    if status not in SAMPLE_STATUSES:
        return {"recorded": False, "store_status": "invalid_row", "reason": f"invalid_status:{status}"}
    if not execution_id or not command_group_key or not environment_fingerprint or not command_hash:
        return {"recorded": False, "store_status": "invalid_row", "reason": "missing_identity_field"}
    for name, value in (("duration_ms", duration_ms), ("applied_timeout_ms", applied_timeout_ms)):
        if value is not None and (not isinstance(value, int) or value < 0):
            return {"recorded": False, "store_status": "invalid_row", "reason": f"invalid_{name}"}

    observed_at = observed_at_utc or datetime.now(timezone.utc).isoformat()

    try:
        conn = _connect(store_path, busy_timeout_seconds=busy_timeout_seconds)
    except sqlite3.OperationalError as exc:
        return {"recorded": False, "store_status": "locked", "reason": str(exc)}
    except sqlite3.DatabaseError as exc:
        return {"recorded": False, "store_status": "corrupt", "reason": str(exc)}
    except OSError as exc:
        return {"recorded": False, "store_status": "error", "reason": str(exc)}

    try:
        schema_status = _ensure_schema(conn)
        if schema_status != "ok":
            return {"recorded": False, "store_status": schema_status, "reason": "schema_version_mismatch"}
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO samples (
                sample_id, command_group_key, environment_fingerprint,
                observed_at_utc, status, duration_ms, applied_timeout_ms,
                command_hash, writer_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                command_group_key,
                environment_fingerprint,
                observed_at,
                status,
                duration_ms,
                applied_timeout_ms,
                command_hash,
                writer_version,
            ),
        )
        conn.commit()
        recorded = cursor.rowcount > 0
        return {
            "recorded": recorded,
            "store_status": "ok",
            "reason": None if recorded else "duplicate_sample_id_ignored",
        }
    except sqlite3.OperationalError as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        message = str(exc)
        store_status = "locked" if "locked" in message.lower() else "error"
        return {"recorded": False, "store_status": store_status, "reason": message}
    except sqlite3.DatabaseError as exc:
        return {"recorded": False, "store_status": "corrupt", "reason": str(exc)}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# Reader: nearest_rank_v1 percentile + timeout backoff (Issue #2254 AC3/AC5)
# ---------------------------------------------------------------------------


def _row_is_valid(row: Tuple[Any, ...]) -> bool:
    (_group, _fp, observed_at, status, duration_ms, applied_timeout_ms) = row
    if status not in SAMPLE_STATUSES:
        return False
    if not isinstance(observed_at, str) or not observed_at:
        return False
    if duration_ms is not None and (not isinstance(duration_ms, int) or duration_ms < 0):
        return False
    if applied_timeout_ms is not None and (
        not isinstance(applied_timeout_ms, int) or applied_timeout_ms < 0
    ):
        return False
    return True


def compute_history_estimate(
    conn: sqlite3.Connection,
    command_group_key: str,
    environment_fingerprint: str,
    *,
    now_utc: datetime,
) -> Dict[str, Any]:
    """`nearest_rank_v1` (Issue #2254 AC3, fixed contract):

    eligible samples = exit-0 `success` samples only, same
    `environment_fingerprint`, `observed_at_utc` within `HISTORY_TTL_DAYS`,
    newest `HISTORY_WINDOW_MAX_SAMPLES` rows. `failed` (non-zero exit) and
    `cancelled` samples never contribute to this percentile (Issue #2254
    In Scope). Returns
    `eligible_sample_count == 0` (`observed_p95_ms`/`history_candidate_seconds`:
    `None`) if fewer than `HISTORY_MIN_SUCCESS_SAMPLES` eligible rows exist.

    `P95 = sorted_samples[ceil(0.95 * n) - 1]`;
    `candidate_seconds = ceil(observed_p95_ms * 1.5 / 1000)`.
    """
    cutoff = (now_utc - timedelta(days=HISTORY_TTL_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT command_group_key, environment_fingerprint, observed_at_utc,
               status, duration_ms, applied_timeout_ms
        FROM samples
        WHERE command_group_key = ? AND environment_fingerprint = ?
          AND status = 'success' AND observed_at_utc >= ?
        ORDER BY observed_at_utc DESC
        LIMIT ?
        """,
        (command_group_key, environment_fingerprint, cutoff, HISTORY_WINDOW_MAX_SAMPLES),
    ).fetchall()
    valid_durations = [
        row[4] for row in rows if _row_is_valid(row) and row[4] is not None
    ]
    n = len(valid_durations)
    if n < HISTORY_MIN_SUCCESS_SAMPLES:
        return {
            "eligible_sample_count": n,
            "observed_p95_ms": None,
            "history_candidate_seconds": None,
        }
    durations_sorted = sorted(valid_durations)
    rank_index = math.ceil(0.95 * n) - 1
    observed_p95_ms = durations_sorted[rank_index]
    # Issue #2254 fix_delta P0 blocker 7 (OWNER REQUEST_CHANGES
    # source-repo PR 2382 review comment 5458281756):
    # the Issue's fixed AC3 contract pins INTEGER arithmetic
    # (`candidate_seconds = ceil(observed_p95_ms * 1.5 / 1000)`, computed
    # without floating point) -- the prior `math.ceil(p95_ms * 1.5 / 1000)`
    # used float division, which can round incorrectly for values outside
    # float64's exact-integer range. `HISTORY_SAFETY_MARGIN_MULTIPLIER`
    # (1.5) is applied as the exact ratio 3/2000 (`p95_ms * 1.5 / 1000 ==
    # p95_ms * 3 / 2000`), and `ceil(a / b)` for non-negative integers is
    # computed via the standard `(a + b - 1) // b` integer-ceiling-division
    # identity -- never `math.ceil()` on a float.
    candidate_seconds = (observed_p95_ms * 3 + 1999) // 2000
    return {
        "eligible_sample_count": n,
        "observed_p95_ms": observed_p95_ms,
        "history_candidate_seconds": candidate_seconds,
    }


def compute_timeout_backoff_floor(
    conn: sqlite3.Connection,
    command_group_key: str,
    environment_fingerprint: str,
    *,
    now_utc: datetime,
    max_seconds: int,
) -> Dict[str, Any]:
    """Right-censored lower-bound backoff (Issue #2254 In Scope / AC5):
    `timeout_backoff_floor = min(max_seconds, previous_applied_timeout_seconds * 2)`
    from the MOST RECENT `timed_out` sample within the TTL window. A
    `timed_out` sample means the true duration is UNKNOWN but strictly
    greater than `applied_timeout_ms` -- so it is never used to compute a
    duration PERCENTILE (see `compute_history_estimate()`), only this
    separate additive backoff floor."""
    cutoff = (now_utc - timedelta(days=HISTORY_TTL_DAYS)).isoformat()
    row = conn.execute(
        """
        SELECT applied_timeout_ms FROM samples
        WHERE command_group_key = ? AND environment_fingerprint = ?
          AND status = 'timed_out' AND observed_at_utc >= ?
          AND applied_timeout_ms IS NOT NULL AND applied_timeout_ms >= 0
        ORDER BY observed_at_utc DESC
        LIMIT 1
        """,
        (command_group_key, environment_fingerprint, cutoff),
    ).fetchone()
    if row is None or row[0] is None:
        return {"previous_applied_timeout_seconds": None, "timeout_backoff_floor_seconds": None}
    previous_applied_timeout_seconds = math.ceil(row[0] / 1000)
    floor = min(max_seconds, previous_applied_timeout_seconds * 2)
    return {
        "previous_applied_timeout_seconds": previous_applied_timeout_seconds,
        "timeout_backoff_floor_seconds": floor,
    }


# ---------------------------------------------------------------------------
# Immutable snapshot producer (Issue #2254 AC1)
# ---------------------------------------------------------------------------


def _canonicalize_snapshot_body(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def empty_history_snapshot(store_status: str, *, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """A degraded, empty snapshot (Issue #2254 AC8: non-blocking
    degradation). `records == {}` means every command falls back to
    `static_policy`/`static_fallback`."""
    now_utc = now_utc or datetime.now(timezone.utc)
    body = {
        "schema": HISTORY_SNAPSHOT_SCHEMA,
        "snapshot_as_of_utc": now_utc.isoformat(),
        "store_status": store_status,
        "records": {},
    }
    digest = hashlib.sha256(_canonicalize_snapshot_body(body)).hexdigest()
    body["snapshot_digest"] = f"sha256:{digest}"
    return body


def build_history_snapshot(
    store_path: Path,
    group_key_fingerprint_pairs: Iterable[Tuple[str, str]],
    *,
    max_seconds: int,
    now_utc: Optional[datetime] = None,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Root-owned, SINGLE read of `store_path` (Issue #2254 AC1: this
    function is called EXACTLY ONCE per canonical-VC-plan computation by
    the process that owns that computation; every consumer downstream
    receives the resulting dict/file, never calls this function again for
    the SAME plan).

    `group_key_fingerprint_pairs`: the set of `(command_group_key,
    environment_fingerprint)` pairs the caller's body actually needs an
    estimate for (deduplicated by the caller). Any store-access failure
    (missing file, lock timeout, corruption, unknown schema) degrades the
    ENTIRE snapshot to `empty_history_snapshot()` rather than partially
    failing (Issue #2254 AC8) -- a single store-level failure should not
    require per-command differentiation to fall back safely.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    pairs = list(dict.fromkeys(group_key_fingerprint_pairs))

    if not store_path.exists():
        return empty_history_snapshot("missing", now_utc=now_utc)

    try:
        conn = _connect(store_path, busy_timeout_seconds=busy_timeout_seconds)
    except sqlite3.OperationalError:
        return empty_history_snapshot("locked", now_utc=now_utc)
    except sqlite3.DatabaseError:
        return empty_history_snapshot("corrupt", now_utc=now_utc)
    except OSError:
        return empty_history_snapshot("error", now_utc=now_utc)

    try:
        schema_status = _ensure_schema(conn)
        if schema_status != "ok":
            return empty_history_snapshot(schema_status, now_utc=now_utc)

        # Issue #2254 fix_delta P2 (OWNER warning
        # source-repo PR 2382 review comment 5458281756):
        # `isolation_level=None` (autocommit) means each individual SELECT
        # below would otherwise be its OWN implicit transaction, so a
        # concurrent writer committing BETWEEN two of these SELECTs could
        # make different pairs in the SAME "snapshot" observe different
        # points in time. A short explicit `BEGIN DEFERRED` -> every read
        # -> `COMMIT` gives the whole loop one single consistent read
        # transaction (still lock-free to acquire: DEFERRED only takes a
        # SHARED lock on first read, never blocks a concurrent writer's own
        # `BEGIN IMMEDIATE` from starting up until this reader is done).
        records: Dict[str, Any] = {}
        conn.execute("BEGIN DEFERRED")
        try:
            for group_key, fingerprint in pairs:
                estimate = compute_history_estimate(conn, group_key, fingerprint, now_utc=now_utc)
                backoff = compute_timeout_backoff_floor(
                    conn, group_key, fingerprint, now_utc=now_utc, max_seconds=max_seconds
                )
                records[group_key] = {
                    "environment_fingerprint": fingerprint,
                    **estimate,
                    **backoff,
                }
        finally:
            conn.execute("COMMIT")

        body = {
            "schema": HISTORY_SNAPSHOT_SCHEMA,
            "snapshot_as_of_utc": now_utc.isoformat(),
            "store_status": "ok",
            "records": records,
        }
        digest = hashlib.sha256(_canonicalize_snapshot_body(body)).hexdigest()
        body["snapshot_digest"] = f"sha256:{digest}"
        return body
    except sqlite3.OperationalError:
        return empty_history_snapshot("locked", now_utc=now_utc)
    except sqlite3.DatabaseError:
        return empty_history_snapshot("corrupt", now_utc=now_utc)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# Snapshot (de)serialization for cross-process propagation
# ---------------------------------------------------------------------------


def write_history_snapshot_file(snapshot: Dict[str, Any], path: Path) -> None:
    """Issue #2254 fix_delta P1 (OWNER REQUEST_CHANGES
    source-repo PR 2382 review comment 5458281756
    blocker 5 item 3): atomic temp-file + `os.replace()` write -- a plain
    `Path.write_text()` can leave a PARTIALLY-written file visible at
    `path` (e.g. a disk error, or an unlucky concurrent read racing the
    write) that a child process reading via `load_history_snapshot_file()`
    could observe mid-write. Writing to a same-directory sibling temp file
    first and only then atomically renaming it into place (`os.replace()`
    is atomic on POSIX for same-filesystem renames) guarantees a reader of
    `path` always sees either the COMPLETE previous file (before this call)
    or the COMPLETE new one -- never a truncated/partial one."""
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp_path.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)


_SNAPSHOT_RECORD_OPTIONAL_NONNEGATIVE_INT_FIELDS = (
    "observed_p95_ms",
    "history_candidate_seconds",
    "previous_applied_timeout_seconds",
    "timeout_backoff_floor_seconds",
)


def _is_valid_snapshot_record(record: Any) -> bool:
    """Issue #2254 fix_delta P1 blocker 5 item 1: per-record structural
    validation for a loaded `history_snapshot/v1` -- every field a
    `compute_command_timeout_budget()` lookup actually reads must have the
    right TYPE and a sane RANGE before that lookup ever touches it."""
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("environment_fingerprint"), str):
        return False
    eligible = record.get("eligible_sample_count")
    if not isinstance(eligible, int) or isinstance(eligible, bool) or eligible < 0:
        return False
    for field in _SNAPSHOT_RECORD_OPTIONAL_NONNEGATIVE_INT_FIELDS:
        value = record.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    return True


def _is_valid_snapshot_body(data: Any) -> bool:
    """Issue #2254 fix_delta P1 (OWNER REQUEST_CHANGES
    source-repo PR 2382 review comment 5458281756
    blocker 5 item 1): full structural validation of a `history_snapshot/v1`
    body -- not just the top-level dict-ness/schema-name check the
    pre-fix_delta loader performed. A malformed cross-process transport
    (truncated write, a non-dict `records`, a `records` VALUE with the
    wrong nested types/ranges, or a tampered/incorrect `snapshot_digest`)
    must degrade to `empty_history_snapshot("corrupt")` -- the SAME
    non-blocking fallback every other store-access failure mode uses
    (AC8) -- rather than let a malformed dict reach
    `compute_command_timeout_budget()`'s `.get()`/arithmetic and raise."""
    if not isinstance(data, dict):
        return False
    if data.get("schema") != HISTORY_SNAPSHOT_SCHEMA:
        return False
    if not isinstance(data.get("snapshot_as_of_utc"), str):
        return False
    if not isinstance(data.get("store_status"), str):
        return False
    records = data.get("records")
    if not isinstance(records, dict):
        return False
    for group_key, record in records.items():
        if not isinstance(group_key, str):
            return False
        if not _is_valid_snapshot_record(record):
            return False
    digest = data.get("snapshot_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return False
    body_without_digest = {k: v for k, v in data.items() if k != "snapshot_digest"}
    expected_digest = "sha256:" + hashlib.sha256(
        _canonicalize_snapshot_body(body_without_digest)
    ).hexdigest()
    return digest == expected_digest


def load_history_snapshot_file(path: Path) -> Dict[str, Any]:
    """Load a snapshot a parent process serialized via
    `write_history_snapshot_file()`. NEVER raises for a malformed file --
    degrades to `empty_history_snapshot("corrupt")` instead (Issue #2254
    AC8: a corrupt cross-process handoff must degrade like any other store
    failure, not crash the child). Issue #2254 fix_delta P1 blocker 5 item
    1: validates the FULL structure (schema, nested record types/ranges,
    and `snapshot_digest` integrity), not merely the top-level dict-ness/
    schema-name check the pre-fix_delta loader performed."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return empty_history_snapshot("corrupt")
    if not _is_valid_snapshot_body(data):
        return empty_history_snapshot("corrupt")
    return data
