#!/usr/bin/env python3
"""
isolation_issue_comment_bridge.py

Issue #1633 / #1639: parent-owned producer/consumer bridge for the
isolation worktree agent's bounded Issue comment request
(ISOLATION_ISSUE_COMMENT_REQUEST_V1 -> ISSUE_COMMENT_PUBLISH_INPUT_V1 ->
controlled_skill_mutation_exec.py --command-id issue_comment.publish).

This module was extracted from publish_termination_report.py (Issue #1873
Tranche 3) because the termination-report publisher it originally lived in
was removed, but this bridge is an independent, still-production
isolation-worktree `issue_comment.publish` lane and must not be deleted
along with it.

Exposed API:
    build_isolation_issue_comment_request(...)       -- producer
    materialize_isolation_issue_comment_request(...)  -- consumer/materializer
    post_isolation_issue_comment(...)                 -- end-to-end helper that
                                                          builds, materializes,
                                                          and invokes the
                                                          controlled executor.

See docs/dev/agent-skill-boundaries.md for the symlink/TOCTOU-safe write
design (Issue #1639 fix_delta P0) and the producer/consumer boundary
rationale (Issue #1639 fix_delta P1-1).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat as _stat
import subprocess
import sys
from pathlib import Path
from typing import cast

# ---------------------------------------------------------------------------
# Issue #1633: shared controlled-executor policy import (bounded request
# schema + issue_comment.publish command id / namespace / input schema).
# scripts/agent-guards is a sibling top-level directory, not a package --
# resolved by absolute path from this file, independent of cwd.
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_AGENT_GUARDS_DIR = _PROJECT_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from controlled_skill_mutation_policy import (  # noqa: E402
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    INPUT_SCHEMA_BY_COMMAND,
    ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA,
    ISSUE_METADATA_NAMESPACE_SEGMENT,
    validate_isolation_issue_comment_request,
)

CONTROLLED_SKILL_MUTATION_EXEC_SCRIPT = _AGENT_GUARDS_DIR / "controlled_skill_mutation_exec.py"


# ---------------------------------------------------------------------------
# Issue #1639 fix_delta (P0): symlink/TOCTOU-safe atomic write helpers.
#
# The materializer below runs on the canonical main root with full
# filesystem permissions. A naive implementation that wrote the
# materialized ISSUE_COMMENT_PUBLISH_INPUT_V1 JSON via plain
# Path.write_text() would:
#   - follow symlinks at every path component (namespace dir ancestors,
#     the namespace dir itself, and the final output filename), and
#   - truncate-in-place whatever the destination resolves to.
#
# A pre-planted symlink at the predictable output path (or at any ancestor
# directory component) could therefore redirect this write to overwrite an
# arbitrary file the process has write access to (CWE-59/CWE-61), and the
# check-then-write shape was inherently TOCTOU-vulnerable (CWE-363).
#
# The helpers below close this by:
#   1. Walking from project_root down to the namespace directory one path
#      component at a time using dir_fd-relative operations (mkdirat /
#      fstatat / openat via os.mkdir/os.stat/os.open with dir_fd=), so no
#      component is ever resolved through a symlink. mkdir() is race-free:
#      if an attacker has already planted *any* dirent (including a
#      symlink) at that name, mkdir() fails with EEXIST rather than
#      following it; the subsequent lstat + O_NOFOLLOW open then reject
#      anything that isn't a genuine, real directory.
#   2. Writing the payload to a brand-new temp file created with
#      O_CREAT | O_EXCL | O_NOFOLLOW (dir_fd-relative) -- this can never
#      collide with (or follow) a pre-existing dirent.
#   3. Performing the final publish via os.rename() with src_dir_fd/
#      dst_dir_fd -- POSIX rename() never follows symlinks at the
#      destination; it atomically replaces the directory entry itself
#      (whatever it currently is), so even a symlink planted in the race
#      window between the pre-check and the rename cannot redirect the
#      write through it. This is the actual TOCTOU-safe defense; the
#      pre-check below is a fail-closed classification/audit layer on top.
#   4. Rejecting (fail-closed, without touching the target) if the
#      pre-existing destination dirent is a symlink, a directory, a
#      non-regular file (FIFO/device/etc.), or a hardlinked regular file
#      (st_nlink != 1) -- this project's stated policy for this lane is to
#      never silently replace such a dirent, even though rename() itself
#      would not corrupt the symlink/hardlink target in that case.
# ---------------------------------------------------------------------------


class MaterializeSecurityError(Exception):
    """Raised when a filesystem dirent involved in materialization is not a
    safe, expected type (symlink / hardlink / wrong file type / io error)."""


def _resolve_namespace_component_nofollow(parent_fd: int, name: str) -> int:
    """Ensure `name` exists as a real directory directly under parent_fd and
    return a symlink-safe (O_NOFOLLOW) fd for it. Caller must close the
    returned fd. Race-free: mkdir() fails on any pre-existing dirent
    (including a pre-planted symlink) instead of following it; the
    post-creation lstat + O_NOFOLLOW open reject anything that is not a
    genuine directory."""
    try:
        os.mkdir(name, mode=0o777, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise MaterializeSecurityError(f"namespace_mkdir_failed:{name}:{exc}") from exc

    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise MaterializeSecurityError(f"namespace_lstat_failed:{name}:{exc}") from exc

    if _stat.S_ISLNK(st.st_mode):
        raise MaterializeSecurityError(f"namespace_symlink_denied:{name}")
    if not _stat.S_ISDIR(st.st_mode):
        raise MaterializeSecurityError(f"namespace_not_a_directory:{name}")

    try:
        fd = os.open(name, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY, dir_fd=parent_fd)
    except OSError as exc:
        raise MaterializeSecurityError(f"namespace_open_dir_failed:{name}:{exc}") from exc
    return fd


def _write_json_atomic_symlink_safe(
    root: Path, namespace_parts: list[str], filename: str, payload: dict
) -> None:
    """Write `payload` as JSON to root/<namespace_parts>/<filename>,
    race-free and symlink-safe (Issue #1639 fix_delta P0). See the module
    docstring above for the threat model and defense-in-depth design.

    Raises MaterializeSecurityError (bad dirent type) or OSError
    (unexpected I/O failure) on any failure; never partially replaces or
    corrupts a pre-existing legitimate destination file on error, because
    the destination is only ever touched by the final rename() once the
    full replacement temp file has been written and fsync'd successfully.
    """
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    root_fd = os.open(str(root), os.O_DIRECTORY)
    opened_fds = [root_fd]
    try:
        cur_fd = root_fd
        for part in namespace_parts:
            new_fd = _resolve_namespace_component_nofollow(cur_fd, part)
            opened_fds.append(new_fd)
            cur_fd = new_fd

        # Pre-check destination dirent (fail-closed classification/audit;
        # the atomic rename below is the actual TOCTOU-safe defense).
        try:
            dest_st = os.stat(filename, dir_fd=cur_fd, follow_symlinks=False)
        except FileNotFoundError:
            dest_st = None
        except OSError as exc:
            raise MaterializeSecurityError(f"output_lstat_failed:{exc}") from exc

        if dest_st is not None:
            if _stat.S_ISLNK(dest_st.st_mode):
                raise MaterializeSecurityError(f"output_symlink_denied:{filename}")
            if _stat.S_ISDIR(dest_st.st_mode):
                raise MaterializeSecurityError(f"output_is_directory:{filename}")
            if not _stat.S_ISREG(dest_st.st_mode):
                raise MaterializeSecurityError(f"output_not_regular:{filename}")
            if dest_st.st_nlink != 1:
                raise MaterializeSecurityError(
                    f"output_hardlink_denied:st_nlink={dest_st.st_nlink}"
                )

        tmp_name = f".{filename}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
        try:
            tmp_fd = os.open(
                tmp_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o644,
                dir_fd=cur_fd,
            )
        except OSError as exc:
            raise MaterializeSecurityError(f"tmp_create_failed:{exc}") from exc

        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            tmp_st = os.stat(tmp_name, dir_fd=cur_fd, follow_symlinks=False)
            if _stat.S_ISLNK(tmp_st.st_mode) or tmp_st.st_nlink != 1:
                raise MaterializeSecurityError("tmp_file_unexpected_link_state")

            os.rename(tmp_name, filename, src_dir_fd=cur_fd, dst_dir_fd=cur_fd)
        except Exception:
            try:
                os.unlink(tmp_name, dir_fd=cur_fd)
            except OSError:
                pass
            raise
    finally:
        for fd_ in reversed(opened_fds):
            try:
                os.close(fd_)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Issue #1633 / #1639: parent-owned producer/consumer for the isolation
# worktree agent's bounded Issue comment request.
# ---------------------------------------------------------------------------

def build_isolation_issue_comment_request(
    *, issue_number: int, repo: str, comment_body: str, marker: str
) -> dict:
    """Explicit producer for a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1
    request (Issue #1639 fix_delta P1-1).

    This is a separate call site from
    materialize_isolation_issue_comment_request() so the producer (in the
    real flow: an isolation worktree agent handing back only these bounded
    fields) and the consumer (materialize_isolation_issue_comment_request(),
    which validates the closed request object against the caller-declared
    expected_issue_number / expected_repo) are not the same function --
    the validator's bounds checks are therefore an actually-enforced
    boundary, not a code-level tautology.
    """
    return {
        "schema": ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA,
        "issue_number": issue_number,
        "repo": repo,
        "comment_body": comment_body,
        "marker": marker,
    }


def materialize_isolation_issue_comment_request(
    *,
    request: object,
    expected_issue_number: int,
    expected_repo: str,
    project_root: Path | None = None,
) -> tuple[str | None, str]:
    """
    Materialize a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request into the
    issue-scoped input namespace consumed by controlled_skill_mutation_exec.py
    --command-id issue_comment.publish (Issue #1633 / Issue #1608).

    `request` is expected to be the *already-built*, closed-key dict the
    isolation worktree agent produced (see build_isolation_issue_comment_request()
    for the production call site). This function -- run on the canonical
    main root -- validates that object via validate_isolation_issue_comment_request()
    against the caller-declared expected_issue_number / expected_repo (Issue
    #1639 fix_delta P1-1: request is a real, external boundary object here,
    not reconstructed from already-trusted keyword arguments), writes an
    ISSUE_COMMENT_PUBLISH_INPUT_V1 JSON file under
    artifacts/{issue_number}/issue-metadata/issue_comment.publish/ using the
    symlink/TOCTOU-safe atomic write helpers above (Issue #1639 fix_delta
    P0), and returns a project-root-relative POSIX path string so the caller
    can invoke the exact controlled executor argv (the executor rejects
    absolute --input-file paths).

    Returns (relative_input_file_path, error). relative_input_file_path is
    None on validation error or on any materialization security/IO failure.
    """
    root = project_root or _PROJECT_ROOT

    req_err = validate_isolation_issue_comment_request(
        request, expected_issue_number, expected_repo
    )
    if req_err:
        return None, req_err

    validated = cast(dict, request)
    issue_number = validated["issue_number"]
    comment_body = validated["comment_body"]
    marker = validated["marker"]

    materialized = {
        "schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_COMMENT_PUBLISH],
        "issue_number": issue_number,
        "comment_body": comment_body,
        "marker": marker,
    }

    namespace_parts = [
        "artifacts",
        str(issue_number),
        ISSUE_METADATA_NAMESPACE_SEGMENT,
        COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    ]
    filename = "issue_comment_publish_input.json"

    try:
        _write_json_atomic_symlink_safe(root, namespace_parts, filename, materialized)
    except MaterializeSecurityError as exc:
        return None, f"materialize_security_error: {exc}"
    except OSError as exc:
        return None, f"materialize_io_error: {exc}"

    rel_path = "/".join([*namespace_parts, filename])
    return rel_path, ""


# ---------------------------------------------------------------------------
# GitHub comment posting (end-to-end helper for production callers)
# ---------------------------------------------------------------------------

def post_isolation_issue_comment(
    *, issue_number: int, body: str, repo: str, exec_marker: str | None = None
) -> int:
    """
    Post body as a GitHub issue comment via the issue_comment.publish
    controlled mutation lane (Issue #1633).

    Builds a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request (via
    build_isolation_issue_comment_request(), Issue #1639 fix_delta P1-1)
    from body (embedding exec_marker, or a deterministic marker derived
    from repo + issue_number + body when unset -- Issue #1639 fix_delta
    P1-2 -- as the request's marker field), materializes it via
    materialize_isolation_issue_comment_request(), and launches
    controlled_skill_mutation_exec.py --command-id
    issue_comment.publish with the exact argv it accepts (Issue #1166
    AC4/AC17 shared authority -- raw `gh issue comment` is never called
    directly from this module).
    Enforces a 30-second timeout; on timeout fails closed.

    Returns the executor's exit code (0 on success, -1 on timeout, or the
    executor's nonzero exit on failure).
    """
    marker_seed = exec_marker if exec_marker is not None else os.environ.get(
        "CONTROLLED_EXEC_MARKER", ""
    )
    if marker_seed:
        marker = f"<!-- CONTROLLED_EXEC_MARKER:{marker_seed} -->"
    else:
        # Issue #1639 fix_delta P1-2: the fallback marker must not collide
        # across different repos/issues that happen to share identical body
        # content -- hash repo + issue_number + body (NUL-separated to avoid
        # ambiguous concatenation), not body alone.
        fallback_seed = f"{repo}\x00{issue_number}\x00{body}".encode("utf-8")
        content_hash = hashlib.sha256(fallback_seed).hexdigest()[:32]
        marker = f"<!-- CONTROLLED_EXEC_MARKER:{content_hash} -->"
    comment_body = body + f"\n{marker}"

    request = build_isolation_issue_comment_request(
        issue_number=issue_number, repo=repo, comment_body=comment_body, marker=marker,
    )
    materialized_rel_path, materialize_err = materialize_isolation_issue_comment_request(
        request=request, expected_issue_number=issue_number, expected_repo=repo,
    )
    if materialize_err:
        print(
            f"[isolation_issue_comment_bridge] materialize_isolation_issue_comment_request "
            f"failed: {materialize_err}",
            file=sys.stderr,
        )
        return 1

    cmd = [
        sys.executable, str(CONTROLLED_SKILL_MUTATION_EXEC_SCRIPT),
        "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        "--issue-number", str(issue_number),
        "--input-file", materialized_rel_path,
        "--repo", repo,
    ]
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(
            "[isolation_issue_comment_bridge] controlled_skill_mutation_exec "
            "issue_comment.publish timed out (30s) — fail-closed",
            file=sys.stderr,
        )
        return -1

    if proc.returncode != 0:
        print(
            f"[isolation_issue_comment_bridge] controlled_skill_mutation_exec "
            f"issue_comment.publish failed (exit {proc.returncode}): {proc.stderr[:200]}",
            file=sys.stderr,
        )
    return proc.returncode
