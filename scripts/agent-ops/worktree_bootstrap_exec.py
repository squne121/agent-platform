#!/usr/bin/env python3
"""Controlled executor for implementation worktree bootstrap (Issue #1209)."""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_GUARDS_DIR = _ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from typing import Self

from skill_runtime_command_policy import (
    is_trusted_repo_slug,
    resolve_default_branch,
    resolve_repo_slug,
    validate_detached_worktree_path,
    validate_existing_detached_worktree_path,
    validate_literal_remote_url,
)
from skill_runtime_exec import (
    CONTROL_PLANE_CANONICAL_REMOTE_URL,
    ControlPlaneUnavailable,
    GitProtocolDeadline,
    resolve_git_subprocess_executable,
    run_control_plane_git_add_detached_locked_worktree,
    run_control_plane_git_delete_private_ref_cas,
    run_control_plane_git_list_worktrees_porcelain_locked_prunable,
    run_control_plane_git_read_worktree_head,
    run_control_plane_git_read_worktree_status_porcelain,
    run_control_plane_git_remove_existing_detached_locked_worktree,
    run_control_plane_remote_default_ref_binding,
    sanitized_git_subprocess_env,
)
from worktree_bootstrap_command_policy import (
    _is_valid_slug,
    expected_branch_name,
    expected_worktree_path,
    normalize_default_branch_ref,
)
from worktree_catalog import (
    branch_short_name,
    find_by_realpath,
    list_worktrees,
    parse_worktree_porcelain_locked_prunable_z,
)

SCHEMA = "WORKTREE_BOOTSTRAP_RESULT_V1"

# This is a coordination primitive, not a security boundary or a Git worktree
# management lock. The identity and anchor are deliberately fixed: callers can
# select a repository root, but cannot select an argv, lock identity, or path.
_CONTROL_PLANE_PREFLIGHT_LOCK_IDENTITY = "control-plane-preflight"
_CONTROL_PLANE_PREFLIGHT_LOCK_RELATIVE_PATH = (
    Path("agent-platform") / "locks" / f"{_CONTROL_PLANE_PREFLIGHT_LOCK_IDENTITY}.lock"
)
_CONTROL_PLANE_PREFLIGHT_LOCK_RETRY_SECONDS = 0.05


class ControlPlaneLifecycleUnavailable(RuntimeError):
    """The fixed lifecycle mutex could not establish its repository scope."""


class ControlPlaneLifecycleLockBusy(ControlPlaneLifecycleUnavailable):
    """The bounded lifecycle-mutex wait reached its monotonic deadline."""

    status = "control_plane_unavailable"
    reason_code = "lifecycle_lock_busy"


class ForeignProcessGuardError(RuntimeError):
    """A guard inherited by a forked process cannot operate on its lock."""


class ReleasedControlPlaneLifecycleGuardError(RuntimeError):
    """A released guard cannot be asserted or released again."""


class ControlPlaneLifecycleGuardKeyMismatch(RuntimeError):
    """A guard whose fixed canonical-common-dir binding changed is rejected."""


class ControlPlaneLifecycleGuardTokenMismatch(RuntimeError):
    """A guard not registered with its acquisition token is rejected."""


class ControlPlaneLifecycleReentrantAcquireError(RuntimeError):
    """The fixed lifecycle mutex is deliberately non-reentrant per process."""


class _OwnershipToken:
    """Opaque, process-local ownership evidence; it cannot be serialized."""

    def __reduce__(self) -> object:
        raise TypeError("control_plane_lifecycle_token_is_not_serializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("control_plane_lifecycle_token_is_not_serializable")


_HELD_CONTROL_PLANE_GUARDS: dict[
    str, tuple[_OwnershipToken, ControlPlanePreflightLifecycleGuard]
] = {}
_ACQUIRING_CONTROL_PLANE_KEYS: set[str] = set()
_CONTROL_PLANE_GUARD_REGISTRY_LOCK = threading.Lock()


def _after_control_plane_mutex_fork_child() -> None:
    """Discard inherited mutex state in an explicit fork child.

    ``flock`` ownership belongs to the shared open file description copied by
    ``fork``.  The child must therefore plain-close only its copy; ``LOCK_UN``
    here would also release the parent's lock.  The copied guards and lock are
    invalid in the new process and must not be retained as registry authority.
    """
    global _HELD_CONTROL_PLANE_GUARDS
    global _ACQUIRING_CONTROL_PLANE_KEYS
    global _CONTROL_PLANE_GUARD_REGISTRY_LOCK

    for _, guard in tuple(_HELD_CONTROL_PLANE_GUARDS.values()):
        fd = guard._fd
        guard._fd = -1
        guard._released = True
        try:
            os.close(fd)
        except OSError:
            pass
    _HELD_CONTROL_PLANE_GUARDS = {}
    _ACQUIRING_CONTROL_PLANE_KEYS = set()
    _CONTROL_PLANE_GUARD_REGISTRY_LOCK = threading.Lock()


os.register_at_fork(after_in_child=_after_control_plane_mutex_fork_child)


def _validate_deadline_at(deadline_at: float) -> float:
    try:
        value = float(deadline_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("control_plane_lifecycle_deadline_invalid") from exc
    if not math.isfinite(value):
        raise ValueError("control_plane_lifecycle_deadline_invalid")
    return value


def _raise_if_deadline_expired(deadline_at: float) -> float:
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise ControlPlaneLifecycleLockBusy("control_plane_unavailable/lifecycle_lock_busy")
    return remaining


def _canonical_existing_git_common_dir(
    project_root: str | os.PathLike[str], *, deadline_at: float
) -> Path:
    """Derive the only permitted mutex scope with a fixed trusted Git command.

    The command has no caller-provided argv and inherits neither repository
    selecting ``GIT_*`` variables nor an untrusted Git executable. Git's
    ``--path-format=absolute`` is the authority for the absolute result; the
    filesystem resolution below establishes the required existing realpath.
    """
    root = os.path.realpath(os.fspath(project_root))
    _raise_if_deadline_expired(deadline_at)
    try:
        result = subprocess.run(  # noqa: PLW1510
            [
                resolve_git_subprocess_executable(root),
                "--no-replace-objects",
                "-C",
                root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=_raise_if_deadline_expired(deadline_at),
            shell=False,
            env=sanitized_git_subprocess_env(root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlPlaneLifecycleUnavailable("canonical_git_common_dir_unavailable") from exc
    if result.returncode != 0:
        raise ControlPlaneLifecycleUnavailable("canonical_git_common_dir_unavailable")
    try:
        common_dir = Path(result.stdout.rstrip("\n")).resolve(strict=True)
    except OSError as exc:
        raise ControlPlaneLifecycleUnavailable("canonical_git_common_dir_unavailable") from exc
    if not common_dir.is_dir():
        raise ControlPlaneLifecycleUnavailable("canonical_git_common_dir_unavailable")
    return common_dir


def _fixed_control_plane_lock_path(canonical_common_dir: Path) -> Path:
    return canonical_common_dir / _CONTROL_PLANE_PREFLIGHT_LOCK_RELATIVE_PATH


class ControlPlanePreflightLifecycleGuard:
    """One owner-PID-bound acquisition of the fixed control-plane mutex."""

    def __init__(
        self, *, canonical_common_dir: Path, lock_path: Path, fd: int, token: _OwnershipToken
    ) -> None:
        self._canonical_common_dir = canonical_common_dir
        self._lock_path = lock_path
        self._fd = fd
        self._token = token
        self._owner_pid = os.getpid()
        self._released = False

    @property
    def canonical_common_dir(self) -> Path:
        return self._canonical_common_dir

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def _assert_current_owner_and_binding(self) -> None:
        if os.getpid() != self._owner_pid:
            raise ForeignProcessGuardError("control_plane_lifecycle_guard_foreign_process")
        if self._released:
            raise ReleasedControlPlaneLifecycleGuardError("control_plane_lifecycle_guard_released")
        expected_lock_path = _fixed_control_plane_lock_path(self._canonical_common_dir)
        if self._lock_path != expected_lock_path:
            raise ControlPlaneLifecycleGuardKeyMismatch(
                "control_plane_lifecycle_guard_key_mismatch"
            )
        key = str(self._canonical_common_dir)
        with _CONTROL_PLANE_GUARD_REGISTRY_LOCK:
            registered = _HELD_CONTROL_PLANE_GUARDS.get(key)
        if registered != (self._token, self):
            raise ControlPlaneLifecycleGuardTokenMismatch(
                "control_plane_lifecycle_guard_token_mismatch"
            )

    def assert_held(self) -> None:
        """Confirm owner PID, token, fixed scope, and unreleased state only."""
        self._assert_current_owner_and_binding()

    def release(self) -> None:
        """Explicitly release this owner's kernel lock and close its FD."""
        self._assert_current_owner_and_binding()
        key = str(self._canonical_common_dir)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self._fd)
            finally:
                self._released = True
                with _CONTROL_PLANE_GUARD_REGISTRY_LOCK:
                    if _HELD_CONTROL_PLANE_GUARDS.get(key) == (self._token, self):
                        del _HELD_CONTROL_PLANE_GUARDS[key]

    def __enter__(self) -> Self:
        self.assert_held()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        del exc_type, exc_value, traceback
        self.release()
        return False


def acquire_control_plane_preflight_lifecycle_mutex(
    project_root: str | os.PathLike[str], *, deadline_at: float
) -> ControlPlanePreflightLifecycleGuard:
    """Acquire the fixed common-dir mutex before any remote identity observation.

    The caller supplies only the repository root and an absolute monotonic
    deadline. Contention uses ``LOCK_EX | LOCK_NB`` retries; it never performs
    TTL, heartbeat, stale scanning, takeover, or lock-file unlinking.
    """
    deadline = _validate_deadline_at(deadline_at)
    canonical_common_dir = _canonical_existing_git_common_dir(project_root, deadline_at=deadline)
    lock_path = _fixed_control_plane_lock_path(canonical_common_dir)
    key = str(canonical_common_dir)
    with _CONTROL_PLANE_GUARD_REGISTRY_LOCK:
        if key in _HELD_CONTROL_PLANE_GUARDS or key in _ACQUIRING_CONTROL_PLANE_KEYS:
            raise ControlPlaneLifecycleReentrantAcquireError(
                "control_plane_lifecycle_mutex_not_reentrant"
            )
        _ACQUIRING_CONTROL_PLANE_KEYS.add(key)

    fd: int | None = None
    try:
        _raise_if_deadline_expired(deadline)
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise ControlPlaneLifecycleUnavailable(
                        "control_plane_lifecycle_lock_unavailable"
                    ) from exc
                remaining = _raise_if_deadline_expired(deadline)
                time.sleep(min(_CONTROL_PLANE_PREFLIGHT_LOCK_RETRY_SECONDS, remaining))
        token = _OwnershipToken()
        guard = ControlPlanePreflightLifecycleGuard(
            canonical_common_dir=canonical_common_dir,
            lock_path=lock_path,
            fd=fd,
            token=token,
        )
        with _CONTROL_PLANE_GUARD_REGISTRY_LOCK:
            _HELD_CONTROL_PLANE_GUARDS[key] = (token, guard)
        fd = None
        return guard
    finally:
        with _CONTROL_PLANE_GUARD_REGISTRY_LOCK:
            _ACQUIRING_CONTROL_PLANE_KEYS.discard(key)
        if fd is not None:
            os.close(fd)


# Issue #2197: the one fixed dedicated worktree identity this module
# recovers/creates/reuses/refreshes -- deliberately the same fixed identity
# string already used for the lifecycle mutex above, and never
# caller-selectable (no `--slug`/`--worktree-path` equivalent exists for
# this path).
_FIXED_CONTROL_PLANE_WORKTREE_RELATIVE_PATH = (
    Path("worktrees") / _CONTROL_PLANE_PREFLIGHT_LOCK_IDENTITY
)


def fixed_control_plane_worktree_path(project_root: str | os.PathLike[str]) -> str:
    """The one fixed dedicated worktree path recovered/created by
    `recover_or_create_fixed_control_plane_worktree` (Issue #2197 AC5), e.g.
    `.claude/worktrees/control-plane-preflight`."""
    root = os.path.realpath(os.fspath(project_root))
    return str(Path(root) / ".claude" / _FIXED_CONTROL_PLANE_WORKTREE_RELATIVE_PATH)


# Fixed, dedicated-worktree-EXTERNAL uv project environment directory (Issue
# #2199 OWNER feedback P1-1). Deliberately a SIBLING of
# `_FIXED_CONTROL_PLANE_WORKTREE_RELATIVE_PATH`, never nested inside it.
_FIXED_CONTROL_PLANE_VENV_RELATIVE_PATH = (
    Path("worktrees") / f"{_CONTROL_PLANE_PREFLIGHT_LOCK_IDENTITY}-venv"
)


def dedicated_execution_venv_dir(project_root: str | os.PathLike[str]) -> str:
    """Fixed, dedicated-worktree-EXTERNAL `uv` project environment directory
    for the 4 production preflight profiles (Issue #2199 OWNER feedback
    P1-1: "managed uv project の初回実行が unauthorized write と誤判定される").

    Deliberately a SIBLING of `fixed_control_plane_worktree_path`'s own
    dedicated worktree, never nested inside it: `skill_runtime_exec`'s
    write-monitoring window (`_snapshot_repo_paths`) walks only
    `execution_root`'s own subtree, so a `uv`-managed project environment
    that lives outside that subtree (via `UV_PROJECT_ENVIRONMENT`, set by
    the caller alongside this path) is structurally invisible to the
    before/after unauthorized-write diff. This is "explicit segregation of
    the execution environment outside the monitored checkout" (one of the
    two remedies the OWNER feedback names) -- not a `.venv`-name exclusion
    list, and not a bare `--no-sync`/`--locked` workaround on an unprepared
    environment.

    Stable across `recover_or_create_fixed_control_plane_worktree`
    recreating the dedicated worktree at a new `accepted_oid` (OWNER
    feedback case 3, "dedicated worktree が再作成された後の初回起動"): only
    the *worktree* is torn down and rebuilt on an OID change, never this
    environment directory, so "first launch after worktree recreation"
    reduces to the ordinary "environment already exists" case rather than a
    second first-run.
    """
    root = os.path.realpath(os.fspath(project_root))
    return str(Path(root) / ".claude" / _FIXED_CONTROL_PLANE_VENV_RELATIVE_PATH)


def recover_or_create_fixed_control_plane_worktree(
    accepted_oid: object,
    object_format: object,
    *,
    project_root: str,
    canonical_common_dir: Path,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> dict[str, str]:
    """Fixed dedicated worktree crash/rerun recovery (Issue #2197 AC5).

    - absent (no path, no catalog entry) -> detached+locked create at
      `accepted_oid`
    - verified dedicated identity (detached, git-common-dir linkage matches
      `canonical_common_dir`, and present on disk) with the *same* accepted
      OID already checked out -> reuse as-is
    - verified identity with a *different* accepted OID, and a clean working
      tree -> controlled remove + recreate (never a generic force-remove --
      see `run_control_plane_git_remove_existing_detached_locked_worktree`)
    - verified identity but a dirty working tree -> fail closed
      (`ControlPlaneUnavailable`), regardless of OID match
    - anything not a verified dedicated identity (not detached, absent from
      disk despite a catalog entry, or a different git-common-dir) -> fail
      closed as an unknown owner / linkage mismatch, never recovered
      automatically
    - a path occupied outside the worktree catalog entirely (not a
      registered worktree at all) -> fail closed as an unknown owner
    """
    fixed_path = fixed_control_plane_worktree_path(project_root)
    catalog = list_worktrees(project_root)
    if catalog is None:
        raise ControlPlaneUnavailable(
            "control_plane_unavailable:fixed_worktree_catalog_unavailable"
        )
    entry = find_by_realpath(catalog, fixed_path)

    if entry is None:
        if os.path.lexists(fixed_path):
            raise ControlPlaneUnavailable("control_plane_unavailable:fixed_worktree_unknown_owner")
        fresh_path = validate_detached_worktree_path(fixed_path, project_root)
        run_control_plane_git_add_detached_locked_worktree(
            fresh_path,
            accepted_oid,
            cwd=project_root,
            project_root=project_root,
            deadline=deadline,
            scratch_root=scratch_root,
        )
        return {"state": "created", "worktree_path": fresh_path.value}

    if (
        not entry.get("detached")
        or not entry.get("exists_on_disk")
        or entry.get("git_common_dir") != str(canonical_common_dir)
    ):
        raise ControlPlaneUnavailable("control_plane_unavailable:fixed_worktree_linkage_mismatch")

    existing_path = validate_existing_detached_worktree_path(
        entry["worktree_realpath"], project_root
    )
    current_head = run_control_plane_git_read_worktree_head(
        existing_path,
        object_format,
        project_root=project_root,
        deadline=deadline,
        scratch_root=scratch_root,
    )
    # Cleanliness is checked *before* the same-OID reuse decision so a dirty
    # working tree fails closed regardless of whether `current_head` happens
    # to already equal `accepted_oid` (Issue #2197 AC5).
    status = run_control_plane_git_read_worktree_status_porcelain(
        existing_path, project_root=project_root, deadline=deadline, scratch_root=scratch_root
    )
    if status.strip():
        raise ControlPlaneUnavailable("control_plane_unavailable:fixed_worktree_dirty")
    if current_head == accepted_oid:
        return {"state": "reused", "worktree_path": existing_path.value}

    run_control_plane_git_remove_existing_detached_locked_worktree(
        existing_path,
        cwd=project_root,
        project_root=project_root,
        deadline=deadline,
        scratch_root=scratch_root,
    )
    fresh_path = validate_detached_worktree_path(fixed_path, project_root)
    run_control_plane_git_add_detached_locked_worktree(
        fresh_path,
        accepted_oid,
        cwd=project_root,
        project_root=project_root,
        deadline=deadline,
        scratch_root=scratch_root,
    )
    return {"state": "refreshed", "worktree_path": fresh_path.value}


def run_control_plane_preflight_session(
    project_root: str | os.PathLike[str],
    *,
    timeout_seconds: float = 30.0,
    cleanup_reserve_seconds: float = 3.0,
    scratch_root: str | None = None,
) -> dict[str, object]:
    """Session/context-manager seam binding the fixed process-scoped
    lifecycle guard to the remote-binding protocol and fixed dedicated
    worktree recovery (Issue #2197 AC6/AC7).

    There is deliberately no caller-supplied remote URL parameter here (Issue
    #2197 AC1): the only remote authority this closed entry point ever binds
    to is the code-owned `CONTROL_PLANE_CANONICAL_REMOTE_URL` constant, read
    from this module's own globals at call time. A test that needs to bind
    against a local fixture remote instead of the real canonical remote does
    so the same way every other test in this codebase isolates a module
    constant: `monkeypatch.setattr(this_module, "CONTROL_PLANE_CANONICAL_REMOTE_URL", ...)`.

    The guard from `acquire_control_plane_preflight_lifecycle_mutex` is
    acquired -- and `assert_held()`-confirmed -- before any remote operation,
    held across the remote-binding protocol
    (`run_control_plane_remote_default_ref_binding`), the fixed worktree
    recovery transition, and every private-ref terminal CAS cleanup those
    steps require, and released only after that bounded terminal cleanup
    completes (success or failure alike -- via `finally`). No lease, TTL,
    heartbeat, stale-takeover, or daemon is introduced; this is exactly the
    already-reviewed guard, held for a wider, precisely bounded scope. This
    function performs no actual child dispatch or artifact validation --
    those belong to #2199 / #2200 (Out of Scope).
    """
    root = os.path.realpath(os.fspath(project_root))
    guard = acquire_control_plane_preflight_lifecycle_mutex(
        root, deadline_at=time.monotonic() + timeout_seconds
    )
    try:
        guard.assert_held()
        literal_remote_url = validate_literal_remote_url(CONTROL_PLANE_CANONICAL_REMOTE_URL)
        protocol_deadline = GitProtocolDeadline.start(timeout_seconds, cleanup_reserve_seconds)
        private_ref, accepted_oid, object_format = run_control_plane_remote_default_ref_binding(
            literal_remote_url,
            cwd=root,
            project_root=root,
            deadline=protocol_deadline,
            scratch_root=scratch_root,
        )
        guard.assert_held()

        def _cleanup_private_ref() -> None:
            try:
                run_control_plane_git_delete_private_ref_cas(
                    private_ref,
                    accepted_oid,
                    cwd=root,
                    project_root=root,
                    deadline=protocol_deadline,
                    scratch_root=scratch_root,
                )
            except BaseException as cleanup_exc:
                raise ControlPlaneUnavailable(
                    "control_plane_unavailable:private_ref_cleanup_failed"
                ) from cleanup_exc

        try:
            recovery = recover_or_create_fixed_control_plane_worktree(
                accepted_oid,
                object_format,
                project_root=root,
                canonical_common_dir=guard.canonical_common_dir,
                deadline=protocol_deadline,
                scratch_root=scratch_root,
            )
        except BaseException:
            _cleanup_private_ref()
            raise
        _cleanup_private_ref()
        guard.assert_held()
        return {
            "status": "ok",
            "worktree_path": recovery["worktree_path"],
            "worktree_state": recovery["state"],
            "accepted_oid": accepted_oid.value,
        }
    finally:
        guard.release()


class DedicatedIdentityViolation(ControlPlaneUnavailable):
    """Fail-closed terminal state for the dedicated-lane identity probe
    (Issue #2199 AC2/AC11). Never resolved by falling back to the primary
    root or by continuing on a different/alternate dedicated identity --
    the caller must abort the production dispatch entirely.
    """


class DedicatedEnvironmentPreparationFailed(ControlPlaneUnavailable):
    """Fail-closed terminal state for the explicit toolchain-preparation
    phase (Issue #2199 OWNER feedback P1-1). Never resolved by falling back
    to an unsynced/partial environment or to the primary root -- the caller
    must abort the production dispatch entirely, exactly like
    `DedicatedIdentityViolation`.
    """


def ensure_dedicated_execution_environment_ready(
    *,
    execution_root: str,
    uv_executable: str,
    env: dict[str, str],
    timeout_seconds: float = 120.0,
) -> None:
    """Explicit toolchain-preparation phase for the 4 production preflight
    profiles (Issue #2199 OWNER feedback P1-1), run by the caller BEFORE the
    write-monitoring window opens (before
    `skill_runtime_exec._dispatch_child_and_check_postconditions`'s own
    before-snapshot).

    Runs ``uv sync --locked`` against the dedicated worktree's OWN
    ``pyproject.toml``/``uv.lock`` (already present there -- both are
    ordinary tracked files, checked out by the worktree like any other repo
    file), targeting the sibling, monitored-tree-EXTERNAL
    `dedicated_execution_venv_dir` via ``env["UV_PROJECT_ENVIRONMENT"]``
    (the caller sets this key before calling here, and must pass the SAME
    ``env`` on to the actual monitored child dispatch afterward).

    ``--locked`` means this never silently rewrites ``uv.lock`` content on
    a resolution drift -- it fails closed instead (surfacing as a non-zero
    ``uv`` exit here, raising `DedicatedEnvironmentPreparationFailed`).
    Running this ONCE, here, before the monitored window opens, is also
    what keeps a subsequent already-synced ``uv run`` (the actual, monitored
    child dispatch) from touching ``uv.lock``'s mtime a second time: `uv`
    only rewrites/touches a lockfile-adjacent path when the environment is
    not already up to date, so this call absorbs that one-time mtime bump
    on the caller's behalf, before the caller ever takes its before-snapshot.

    This function itself never touches `execution_root` outside what `uv`
    itself writes -- it does not create, move, or delete any path.
    """
    try:
        result = subprocess.run(
            [uv_executable, "sync", "--locked"],
            cwd=execution_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DedicatedEnvironmentPreparationFailed(
            "dedicated_environment_preparation_timed_out"
        ) from exc
    if result.returncode != 0:
        raise DedicatedEnvironmentPreparationFailed(
            "dedicated_environment_preparation_failed: "
            f"exit={result.returncode} stderr={result.stderr.strip()[:2000]}"
        )


@contextlib.contextmanager
def control_plane_dedicated_execution_session(
    project_root: str | os.PathLike[str],
    *,
    timeout_seconds: float = 30.0,
    cleanup_reserve_seconds: float = 3.0,
    scratch_root: str | None = None,
):
    """Context manager holding the fixed lifecycle guard across bootstrap,
    dedicated-worktree child dispatch, and existing post-child checks
    (Issue #2199 AC3/AC4).

    Unlike `run_control_plane_preflight_session` (Issue #2197 AC6/AC7, whose
    guard is released -- via its own `finally` -- before that function ever
    returns to its caller), this generator's `with` body runs WHILE the
    SAME guard is still held: the guard is released only in this
    generator's own `finally`, after the caller's `with` block (covering
    child dispatch and post-child checks) completes on both the success and
    the exception path. No lease, TTL, heartbeat, stale-takeover, or daemon
    is introduced -- this is exactly the already-reviewed #2198 guard, held
    for the wider, precisely bounded scope this Issue requires.
    """
    root = os.path.realpath(os.fspath(project_root))
    guard = acquire_control_plane_preflight_lifecycle_mutex(
        root, deadline_at=time.monotonic() + timeout_seconds
    )
    try:
        guard.assert_held()
        literal_remote_url = validate_literal_remote_url(CONTROL_PLANE_CANONICAL_REMOTE_URL)
        protocol_deadline = GitProtocolDeadline.start(timeout_seconds, cleanup_reserve_seconds)
        private_ref, accepted_oid, object_format = run_control_plane_remote_default_ref_binding(
            literal_remote_url,
            cwd=root,
            project_root=root,
            deadline=protocol_deadline,
            scratch_root=scratch_root,
        )
        guard.assert_held()

        def _cleanup_private_ref() -> None:
            try:
                run_control_plane_git_delete_private_ref_cas(
                    private_ref,
                    accepted_oid,
                    cwd=root,
                    project_root=root,
                    deadline=protocol_deadline,
                    scratch_root=scratch_root,
                )
            except BaseException as cleanup_exc:
                raise ControlPlaneUnavailable(
                    "control_plane_unavailable:private_ref_cleanup_failed"
                ) from cleanup_exc

        try:
            recovery = recover_or_create_fixed_control_plane_worktree(
                accepted_oid,
                object_format,
                project_root=root,
                canonical_common_dir=guard.canonical_common_dir,
                deadline=protocol_deadline,
                scratch_root=scratch_root,
            )
        except BaseException:
            _cleanup_private_ref()
            raise
        _cleanup_private_ref()
        guard.assert_held()
        session: dict[str, object] = {
            "guard": guard,
            "execution_root": recovery["worktree_path"],
            "worktree_state": recovery["state"],
            "accepted_oid": accepted_oid,
            "object_format": object_format,
            "canonical_common_dir": guard.canonical_common_dir,
            "deadline": protocol_deadline,
        }
        yield session
        guard.assert_held()
    finally:
        guard.release()


def verify_dedicated_control_plane_identity(
    session: dict[str, object],
    *,
    project_root: str,
    execution_root: str,
    invocation_cwd: str,
    executor_script_path: str,
) -> None:
    """Fail-closed dedicated-lane identity verification (Issue #2199
    AC2/AC10/AC11).

    Confirms, from the porcelain catalog alone (never a second remote
    observation -- AC10 reuses the SAME ``session["accepted_oid"]`` already
    obtained via #2197's remote-binding protocol):

    - exactly one catalog entry matches ``execution_root``'s realpath
    - that entry is ``detached``, ``locked``, and NOT ``prunable``
    - its ``head`` equals ``session["accepted_oid"]``
    - its git-common-dir linkage matches ``session["canonical_common_dir"]``
      (re-derived the same closed way ``acquire_control_plane_preflight_lifecycle_mutex``
      already did, never re-queried ad hoc)
    - ``invocation_cwd``, ``executor_script_path``, and ``execution_root``
      all resolve to the SAME dedicated identity (no symlink path
      component in any of the three, ``invocation_cwd`` equals
      ``execution_root``, and the script path nests under it)

    Any missing/duplicate catalog entry, linkage mismatch, symlink
    component, non-detached head, ``locked==false``, ``prunable==true``, or
    OID mismatch raises ``DedicatedIdentityViolation`` -- fail-closed, with
    no primary fallback and no dedicated-side alternate continuation
    (AC11). Ambient ``LOOP_DEFAULT_BRANCH`` is never consulted here.
    """
    guard = session["guard"]
    accepted_oid = session["accepted_oid"]
    canonical_common_dir = session["canonical_common_dir"]
    deadline = session["deadline"]

    for candidate_path in (execution_root, invocation_cwd, executor_script_path):
        candidate = Path(candidate_path)
        if candidate.is_symlink():
            raise DedicatedIdentityViolation(
                "control_plane_unavailable:dedicated_identity_symlink_component"
            )
        for component in candidate.parents:
            if component.is_symlink():
                raise DedicatedIdentityViolation(
                    "control_plane_unavailable:dedicated_identity_symlink_component"
                )

    execution_root_real = os.path.realpath(execution_root)
    invocation_cwd_real = os.path.realpath(invocation_cwd)
    executor_script_real = os.path.realpath(executor_script_path)

    if invocation_cwd_real != execution_root_real:
        raise DedicatedIdentityViolation(
            "control_plane_unavailable:dedicated_identity_cwd_mismatch"
        )
    try:
        common = os.path.commonpath([execution_root_real, executor_script_real])
    except ValueError:
        raise DedicatedIdentityViolation(
            "control_plane_unavailable:dedicated_identity_script_mismatch"
        ) from None
    if common != execution_root_real:
        raise DedicatedIdentityViolation(
            "control_plane_unavailable:dedicated_identity_script_mismatch"
        )

    porcelain = run_control_plane_git_list_worktrees_porcelain_locked_prunable(
        cwd=project_root,
        project_root=project_root,
        deadline=deadline,
    )
    entries = parse_worktree_porcelain_locked_prunable_z(porcelain)
    matches = [entry for entry in entries if entry.get("worktree_realpath") == execution_root_real]
    if len(matches) != 1:
        raise DedicatedIdentityViolation(
            "control_plane_unavailable:dedicated_identity_catalog_match_not_unique"
        )
    entry = matches[0]
    if not entry.get("detached"):
        raise DedicatedIdentityViolation(
            "control_plane_unavailable:dedicated_identity_not_detached"
        )
    if not entry.get("locked"):
        raise DedicatedIdentityViolation("control_plane_unavailable:dedicated_identity_not_locked")
    if entry.get("prunable"):
        raise DedicatedIdentityViolation("control_plane_unavailable:dedicated_identity_prunable")
    if entry.get("head") != accepted_oid.value:
        raise DedicatedIdentityViolation(
            "control_plane_unavailable:dedicated_identity_oid_mismatch"
        )

    entry_common_dir = _canonical_existing_git_common_dir(
        execution_root_real, deadline_at=time.monotonic() + 10.0
    )
    if entry_common_dir != canonical_common_dir:
        raise DedicatedIdentityViolation(
            "control_plane_unavailable:dedicated_identity_common_dir_mismatch"
        )

    guard.assert_held()


def _result(
    *,
    status: str,
    reason_code: str | None,
    issue_number: int,
    slug: str,
    worktree_path: str,
    branch: str,
    base_ref: str | None,
    head_oid: str | None,
    errors: list[str],
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "status": status,
        "reason_code": reason_code,
        "issue_number": issue_number,
        "slug": slug,
        "worktree_path": worktree_path,
        "branch": branch,
        "base_ref": base_ref,
        "head_oid": head_oid,
        "errors": errors,
    }


def _emit(payload: dict[str, object], exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


def _run_git(
    project_root: str, *args: str, timeout: float = 10.0
) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git") or "git"
    return subprocess.run(  # noqa: PLW1510
        [git, "-C", project_root, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _git_stdout(project_root: str, *args: str, timeout: float = 10.0) -> str | None:
    try:
        result = _run_git(project_root, *args, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _current_branch(project_root: str) -> str | None:
    return _git_stdout(project_root, "branch", "--show-current")


def _is_under(base: str, target: str) -> bool:
    """Return True iff realpath(target) is under realpath(base)."""
    try:
        real_base = os.path.realpath(base)
        real_target = os.path.realpath(target)
        return os.path.commonpath([real_base, real_target]) == real_base
    except ValueError:
        return False


def _validate_repo_root(project_root: str) -> tuple[bool, str | None]:
    toplevel = _git_stdout(project_root, "rev-parse", "--show-toplevel")
    if toplevel is None or os.path.realpath(toplevel) != os.path.realpath(project_root):
        return False, "invalid_repo_root"
    catalog = list_worktrees(project_root)
    if not catalog:
        return False, "worktree_catalog_unavailable"
    primary = catalog[0].get("worktree_realpath")
    if not primary or os.path.realpath(primary) != os.path.realpath(project_root):
        return False, "not_primary_worktree"
    repo_slug = resolve_repo_slug(project_root)
    if not is_trusted_repo_slug(repo_slug):
        return False, "invalid_repo_slug"
    return True, None


def _branch_exists(project_root: str, branch_name: str) -> bool:
    result = _git_stdout(project_root, "rev-parse", "--verify", f"refs/heads/{branch_name}")
    return result is not None


def _validate_existing_state(
    project_root: str,
    issue_number: int,
    slug: str,
    worktree_realpath: str,
    branch_name: str,
) -> tuple[str, str | None]:
    catalog = list_worktrees(project_root)
    if catalog is None:
        return "blocked", "worktree_catalog_unavailable"
    entry = find_by_realpath(catalog, worktree_realpath)
    if entry is not None:
        if branch_short_name(entry.get("branch_ref")) != branch_name:
            return "blocked", "existing_conflict"
        if entry.get("detached"):
            return "blocked", "existing_conflict"
        if not os.path.isdir(worktree_realpath):
            return "blocked", "existing_conflict"
        if os.path.basename(worktree_realpath) != f"issue-{issue_number}-{slug}":
            return "blocked", "existing_conflict"
        return "ok_existing", None

    if os.path.lexists(worktree_realpath):
        return "blocked", "existing_conflict"

    if _branch_exists(project_root, branch_name):
        for candidate in catalog:
            if branch_short_name(candidate.get("branch_ref")) == branch_name:
                return "blocked", "existing_conflict"
        return "blocked", "existing_conflict"
    return "create", None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # B6: --json is mandatory; reject if missing
    if not args.json:
        payload = _result(
            status="blocked",
            reason_code="invalid_args",
            issue_number=0,
            slug=str(args.slug),
            worktree_path=str(args.worktree_path),
            branch=str(args.branch_name),
            base_ref=str(args.base_ref),
            head_oid=None,
            errors=["--json flag is required"],
        )
        return _emit(payload, 1)

    project_root = os.path.realpath(os.getcwd())
    issue_text = str(args.issue_number)
    slug = str(args.slug)
    branch_name = str(args.branch_name)
    worktree_path = str(args.worktree_path)
    base_ref = str(args.base_ref)

    if not issue_text.isdigit() or int(issue_text) <= 0:
        payload = _result(
            status="blocked",
            reason_code="invalid_args",
            issue_number=0,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["issue_number must be a positive integer"],
        )
        return _emit(payload, 1)
    issue_number = int(issue_text)

    expected_path = expected_worktree_path(issue_number, slug)
    expected_branch = expected_branch_name(issue_number, slug)
    normalized_worktree_path = os.path.normpath(worktree_path)
    worktree_realpath = os.path.realpath(os.path.join(project_root, normalized_worktree_path))

    # B3: Symlink escape guard
    worktrees_dir = os.path.join(project_root, ".claude", "worktrees")
    if os.path.islink(worktrees_dir):
        payload = _result(
            status="blocked",
            reason_code="invalid_path",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["'.claude/worktrees' is a symlink — symlink escape rejected"],
        )
        return _emit(payload, 1)
    if not _is_under(project_root, worktree_realpath):
        payload = _result(
            status="blocked",
            reason_code="invalid_path",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["worktree_path realpath escapes project root"],
        )
        return _emit(payload, 1)

    if not _is_valid_slug(slug):
        payload = _result(
            status="blocked",
            reason_code="invalid_args",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["slug must match [a-z0-9][a-z0-9-]{0,63}"],
        )
        return _emit(payload, 1)
    if branch_name != expected_branch:
        payload = _result(
            status="blocked",
            reason_code="invalid_branch",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["branch_name must match worktree-issue-<issue>-<slug>"],
        )
        return _emit(payload, 1)
    if (
        normalized_worktree_path != expected_path
        or worktree_path.startswith("/")
        or "\\" in worktree_path
    ):
        payload = _result(
            status="blocked",
            reason_code="invalid_path",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["worktree_path must be .claude/worktrees/issue-<issue>-<slug>"],
        )
        return _emit(payload, 1)
    try:
        check_ref = _run_git(project_root, "check-ref-format", "--branch", branch_name)
    except (OSError, subprocess.TimeoutExpired):
        check_ref = None
    if check_ref is None or check_ref.returncode != 0:
        payload = _result(
            status="blocked",
            reason_code="invalid_branch",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["branch_name failed git check-ref-format --branch"],
        )
        return _emit(payload, 1)

    repo_ok, repo_reason = _validate_repo_root(project_root)
    if not repo_ok:
        payload = _result(
            status="blocked",
            reason_code="invalid_repo",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=[repo_reason or "invalid repo state"],
        )
        return _emit(payload, 1)

    default_branch = resolve_default_branch(project_root)
    if _current_branch(project_root) != default_branch:
        payload = _result(
            status="blocked",
            reason_code="root_not_default_branch",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["current branch must equal the repository default branch"],
        )
        return _emit(payload, 1)

    normalized_base_ref = normalize_default_branch_ref(base_ref, default_branch)
    if normalized_base_ref is None:
        payload = _result(
            status="blocked",
            reason_code="invalid_base_ref",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["base_ref must normalize to the repository default branch"],
        )
        return _emit(payload, 1)

    state, state_reason = _validate_existing_state(
        project_root,
        issue_number,
        slug,
        worktree_realpath,
        branch_name,
    )
    if state == "ok_existing":
        head_oid = _git_stdout(worktree_realpath, "rev-parse", "HEAD")
        payload = _result(
            status="ok_existing",
            reason_code=None,
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=head_oid,
            errors=[],
        )
        return _emit(payload, 0)
    if state == "blocked":
        payload = _result(
            status="blocked",
            reason_code=state_reason,
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=[state_reason or "existing worktree conflict"],
        )
        return _emit(payload, 1)

    try:
        result = _run_git(
            project_root,
            "worktree",
            "add",
            "--no-guess-remote",
            "-b",
            branch_name,
            normalized_worktree_path,
            normalized_base_ref,
            timeout=20.0,
        )
    except subprocess.TimeoutExpired:
        payload = _result(
            status="failed",
            reason_code="timeout",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["git worktree add timed out"],
        )
        return _emit(payload, 1)
    except OSError:
        payload = _result(
            status="failed",
            reason_code="git_failed",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["git executable failed to start"],
        )
        return _emit(payload, 1)

    if result.returncode != 0:
        payload = _result(
            status="failed",
            reason_code="git_failed",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["git worktree add returned non-zero"],
        )
        return _emit(payload, 1)

    catalog = list_worktrees(project_root)
    entry = find_by_realpath(catalog or [], worktree_realpath)
    # B4: Full post-creation readback — equivalent to _validate_existing_state checks
    creation_invalid = (
        entry is None
        or branch_short_name(entry.get("branch_ref")) != branch_name
        or entry.get("detached")
        or not os.path.isdir(worktree_realpath)
        or os.path.basename(worktree_realpath) != f"issue-{issue_number}-{slug}"
    )
    if creation_invalid:
        payload = _result(
            status="failed",
            reason_code="git_failed",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["created worktree failed post-creation readback validation"],
        )
        return _emit(payload, 1)

    head_oid = _git_stdout(worktree_realpath, "rev-parse", "HEAD")
    payload = _result(
        status="ok_created",
        reason_code=None,
        issue_number=issue_number,
        slug=slug,
        worktree_path=normalized_worktree_path,
        branch=branch_name,
        base_ref=normalized_base_ref,
        head_oid=head_oid,
        errors=[],
    )
    return _emit(payload, 0)


if __name__ == "__main__":
    raise SystemExit(main())
