#!/usr/bin/env python3
"""Exact privileged executor for allowed skill runtime commands (Issue #1154)."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import math
import os
import pwd
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True

# `python -I` drops the script directory from sys.path; the sibling policy modules live here.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_runtime_command_policy import (
    GIT_SUBPROCESS_UNSET_ENV_KEYS,
    INSTEADOF_CONFIG_NAME_REGEXP,
    REGISTRY_REL,
    SKILL_RUNTIME_EXEC_REL,
    AllowedRemoteRef,
    ControlPlanePrivateRef,
    DetachedWorktreePath,
    ExactSkillRuntimeCommand,
    LiteralRemoteUrl,
    RepositoryIdentityError,
    RepositoryObjectFormat,
    RepositoryObjectId,
    _is_safe_issue_artifact_path,
    authority_transport_produce_rejection_reason,
    command_allows_root_no_worktree,
    current_branch,
    is_exact_skill_runtime_anchor_executor_command,
    is_exact_skill_runtime_anchor_fixture_executor_command,
    is_exact_skill_runtime_authority_transport_consume_executor_command,
    is_exact_skill_runtime_contract_update_anchor_executor_command,
    is_exact_skill_runtime_decide_authority_executor_command,
    is_exact_skill_runtime_decide_executor_command,
    is_exact_skill_runtime_executor_command,
    is_exact_skill_runtime_fixture_executor_command,
    is_exact_skill_runtime_owner_reaction_decide_executor_command,
    is_exact_skill_runtime_owner_reaction_decide_fixture_executor_command,
    is_exact_skill_runtime_repair_action_apply_executor_command,
    is_exact_skill_runtime_structural_repair_action_apply_executor_command,
    is_trusted_repo_slug,
    load_registry_entry,
    load_trusted_repo_slug,
    make_control_plane_private_ref,
    resolve_active_issue,
    resolve_default_branch,
    resolve_project_root,
    resolve_repo_slug,
    validate_allowed_remote_ref,
    validate_control_plane_private_ref,
    validate_detached_worktree_path,
    validate_existing_detached_worktree_path,
    validate_literal_remote_url,
    validate_registry_entry,
    validate_repository_object_format,
    validate_repository_object_id,
)

# Roots that other concurrent local sessions/agents/hooks may legitimately
# write to while this executor's own child command is running. Changes under
# these roots must never be attributed to the child command's own subprocess
# (Issue #1343, Issue #1409): the executor only ever runs a single child
# process whose own allowed writes are scoped to the target issue's artifact
# root, so any other concurrent repo-wide drift under these roots is
# unattributable -- it may originate from a peer session/agent (Issue #1343)
# or from this same session's own asynchronous PostToolUse/SubagentStop hook
# machinery (Issue #1409: `.claude/hooks/session_manifest_debounce.mjs` /
# `.claude/hooks/generate_session_manifest_from_hook.mjs` writing under the
# hook-owned subtree `artifacts/session-manifest-runtime/`). Either way, the
# executor cannot distinguish "who" wrote it in stdlib-only race-tolerant
# mode, so this symbol is named for that shared property (unattributable),
# not for a single cause (peer-session).
#
# NOTE: `artifacts/session-manifest-runtime` is the *only* addition for
# Issue #1409 -- the repo-root `artifacts/` directory as a whole remains
# fully audited, because `artifacts/{issue}/issue-metadata/{command-id}/`
# is a controlled-mutation input/marker namespace whose provenance still
# needs to be tracked (OWNER REQUEST_CHANGES on the original repo-wide
# `artifacts/` exclusion proposal, see
# source repository issue 1409).
_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS = (
    ".claude/worktrees",
    ".claude/artifacts/issue-refinement-loop",
    "artifacts/session-manifest-runtime",
    # Issue #1526: `.pytest_cache` (repo-root pytest cacheprovider directory,
    # ignored via `.gitignore:69`). pytest's own `NFPlugin`/`LFPlugin`
    # (registered together in `_pytest/cacheprovider.py`'s `pytest_configure`
    # hookimpl) both write into it from `pytest_sessionfinish`: `NFPlugin`
    # saves `cache/nodeids` on every ordinary session finish, and `LFPlugin`
    # saves `cache/lastfailed` whenever the failed-node-id set changed since
    # the last run (PR #2364 review P2: not unconditionally -- pytest 9.0.3's
    # `LFPlugin.pytest_sessionfinish` compares against the previously saved
    # value first). Third-party plugins can additionally write arbitrary keys
    # under `.pytest_cache/v/cache/` via the public `config.cache` API. None
    # of this is canonical evidence, it is concurrent peer pytest's ordinary
    # generated/disposable cache state that this stdlib-only snapshot diff
    # cannot attribute to the executor child vs. a peer process.
    # `.pytest_cache/CACHEDIR.TAG` and its own auto-generated `.gitignore: *`
    # already self-declare the directory as regenerable -- there is no single
    # stable-identity file here whose symlink/directory/FIFO/socket/device
    # substitution would need typed exact-file protection -- the whole tree
    # is disposable, so the directory-root exclusion class used for the peer
    # roots above is the right shape, not a typed exact-file policy.
    ".pytest_cache",
)

# Issue #1526 PR #2364 review P1-1: pytest's cacheprovider does not create
# `.pytest_cache` directly on a cold start. `Cache._ensure_cache_dir_and_
# supporting_files()` first materializes a `tempfile.TemporaryDirectory(
# prefix="pytest-cache-files-", dir=self._cachedir.parent)` -- i.e. a
# repo-root sibling of `.pytest_cache`, not a path under it -- populates it
# with README.md/.gitignore/CACHEDIR.TAG, and only then renames it onto
# `.pytest_cache`. A genuinely concurrent peer pytest process racing this
# executor's before/after snapshot can therefore leave a real, transient
# `pytest-cache-files-<random>/` directory sitting directly at repo root,
# which the `.pytest_cache` root-exclusion above does not cover. This is the
# same disposable/generated-state class as `.pytest_cache` itself (pytest's
# own atomic-rename implementation detail, never canonical evidence), so it
# gets a narrow, repo-root-only, prefix-matched exemption alongside it rather
# than widening `_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS` (which only does
# exact-string/exact-subtree matching, not prefix globs).
_PYTEST_CACHE_COLD_START_TEMP_DIR_PREFIX = "pytest-cache-files-"


def _is_pytest_cache_cold_start_temp_path(rel_path: str) -> bool:
    """True for a repo-root-only `pytest-cache-files-<random>` transient
    directory (or any path inside it) that pytest's cacheprovider creates
    while atomically materializing `.pytest_cache` on a cold start (PR #2364
    review P1-1). Deliberately top-level-only: a nested
    `sub/pytest-cache-files-x` is never pytest's own cache_dir sibling (its
    `dir=self._cachedir.parent` is always the pytest rootdir, which this
    executor treats as repo root) and must remain fully audited.
    """
    normalized = rel_path.replace(os.sep, "/")
    first_segment = normalized.split("/", 1)[0]
    return first_segment.startswith(_PYTEST_CACHE_COLD_START_TEMP_DIR_PREFIX)


def _race_tolerant_unattributable_roots(project_root: str) -> list[Path]:
    root = Path(project_root)
    return [root / Path(rel) for rel in _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS]


def _is_race_tolerant_unattributable_path(rel_path: str) -> bool:
    normalized = rel_path.replace(os.sep, "/")
    for prefix in _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


# Issue #2161 (native Codex CLI retirement): the typed SubAgent-launch-
# ledger transition policy (Issue #1502) that lived here was removed as
# part of the DELETE_CHAIN -- its sole live producer (the former native
# Codex-agents parity checker script under scripts/) no longer exists.


def _is_symlink_path(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part in ("", os.sep):
            continue
        current = current / part
        if current.exists() and current.is_symlink():
            return True
    return False


# Issue #2393: the two contract-update mutation profiles migrated onto the
# dedicated control-plane runtime. Shared by `_allowed_artifact_roots()`,
# `_snapshot_repo_paths()`'s `tmp/` directory-node exclusion, the P1-A
# cold-worktree `tmp/` pre-creation fix, and the P1-B inner-transaction-result
# evidence lookup below -- kept as ONE source of truth rather than repeating
# the same literal set at each call site.
CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS = frozenset(
    {"contract_update.run.with_anchor", "contract_update.run.with_human_context"}
)

# Issue #2584: a capability independent of
# `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS` above. That frozenset
# stays scoped to its original 2 commands and keeps driving the `tmp/`
# directory-node exemption (`_snapshot_repo_paths()`), the cold-worktree
# `tmp/` pre-creation fix, and the inner-transaction-result evidence lookup
# (`_resolve_inner_transaction_evidence_fields()`) -- none of that behavior
# is touched or extended here. This new frozenset ONLY governs
# `_allowed_artifact_roots()`'s decision to additionally permit
# `artifacts/{issue}/issue-metadata/` as a write root, for every command_id
# whose child dispatch is known to route a real GitHub Issue-body mutation
# through the SAME `edit_issue_txn.py` transaction core (directly, as with
# `repair_action.apply`/`structural_repair_action.apply`'s shared
# `_dispatch_candidate_body_via_edit_txn()`, or via a non-noop
# `authority_transport.consume` path): before this Issue, a successful
# mutation by any of the 3 non-`contract_update.run.*` members here was
# misreported as `unauthorized_write_path` even though the write landed
# inside this exact Issue-scoped metadata directory (false negative).
ISSUE_METADATA_WRITE_COMMAND_IDS = frozenset(
    {
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
        "authority_transport.consume",
        "repair_action.apply",
        "structural_repair_action.apply",
    }
)

# Issue #2584 In Scope item 5 (owner decision): a SECOND capability,
# independent of BOTH `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS` and
# `ISSUE_METADATA_WRITE_COMMAND_IDS` above, though it happens to share the
# SAME 5 command_ids as the latter (every member's child dispatch can reach
# `edit_issue_txn.py`'s transaction core, which uses the repo-root `tmp/`
# directory-node as its transaction-local scratch workspace -- the SAME
# reason `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS` already carries its
# own `tmp/` exemption for its original 2 members). This frozenset governs
# ONLY the `tmp/` directory-node housekeeping exemption (the cold-worktree
# pre-creation in `_dispatch_child_and_check_postconditions()` and the
# directory-node mtime-churn exclusion in `_snapshot_repo_paths()`) for the 3
# additional (non-`contract_update.run.*`) members. It must NEVER be merged
# with `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS` -- every call site
# that consults either frozenset for `tmp/` housekeeping keeps its own
# separate `if command_id in ...` branch, so widening/narrowing one
# frozenset's membership can never silently change the other's behavior.
# `CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS` keeps sole, unmodified
# ownership of every OTHER semantic it already drives (the cold-worktree
# `tmp/` pre-creation's own contract_update-specific comment/rationale, and
# `_resolve_inner_transaction_evidence_fields()`'s inner-transaction-result
# evidence lookup) -- none of that is touched or extended by this frozenset.
TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS = frozenset(
    {
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
        "authority_transport.consume",
        "repair_action.apply",
        "structural_repair_action.apply",
    }
)


def _allowed_artifact_roots(
    project_root: str, issue_number: str, command_id: str = ""
) -> tuple[Path, ...]:
    roots = [Path(project_root) / ".claude" / "artifacts" / "issue-refinement-loop" / issue_number]
    if command_id in ISSUE_METADATA_WRITE_COMMAND_IDS:
        # The existing edit-issue transaction writes its request metadata
        # under this exact Issue-scoped directory.  Do not grant the phase a
        # broader artifacts root or a second Issue's metadata directory.
        roots.append(Path(project_root) / "artifacts" / issue_number / "issue-metadata")
    return tuple(roots)


def _allowed_artifact_root(project_root: str, issue_number: str) -> Path:
    """Legacy single-root accessor for read-only preflight callers."""
    return _allowed_artifact_roots(project_root, issue_number)[0]


def _is_under_allowed_artifact_root(
    project_root: str, issue_number: str, rel_path: str, command_id: str = ""
) -> bool:
    root = Path(project_root)
    target = (root / rel_path).resolve()
    return any(
        target == allowed_root.resolve() or target.is_relative_to(allowed_root.resolve())
        for allowed_root in _allowed_artifact_roots(project_root, issue_number, command_id)
    )


def _git_status_paths(project_root: str) -> set[str]:
    git = shutil.which("git") or "git"
    out = subprocess.run(
        [
            git,
            "-C",
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "-z",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("git_status_failed")
    paths: set[str] = set()
    fields = [field for field in out.stdout.split("\0") if field]
    i = 0
    while i < len(fields):
        field = fields[i]
        if len(field) < 4:
            i += 1
            continue
        path = field[3:]
        if field[0] == "R" or field[1] == "R":
            paths.add(path)
            if i + 1 < len(fields):
                paths.add(fields[i + 1])
                i += 2
                continue
        paths.add(path)
        i += 1
    return paths


# Issue #2199 In Scope: the 4 production preflight profiles whose child
# dispatch cwd is migrated to the #2197 dedicated worktree
# (`execution_root`) by `main()`'s real dispatch-selection logic below.
# Issue #2393 In Scope: `contract_update.run.with_anchor` /
# `contract_update.run.with_human_context` (the two contract-update
# mutation profiles #2199 deliberately excluded) are added to this SAME
# set, routing their child dispatch through the SAME existing
# `control_plane_dedicated_execution_session()` parent-controller -- no new
# re-exec route, command entrypoint, or lock protocol. Fixture profiles
# (`preflight.run.fixture`/`preflight.run.fixture.with_human_context`)
# remain deliberately excluded (AC8/#2199 non-regression) -- they stay on
# the primary root (`canonical_main_root`), unaffected by either Issue.
PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS = frozenset(
    {
        "preflight.run",
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
    }
)

# Issue #2200 In Scope: the new artifact-confinement/bounded-cleanup/
# generation-invalidation semantics apply ONLY to the 4 production preflight
# profiles, never to the 2 contract_update mutation profiles (those keep
# ONLY the pre-existing PR #2520/#2533 inner-transaction-result-preservation
# contract, unmodified by this Issue). Computed as a set difference from the
# two frozensets above so this can never silently drift from either one.
ARTIFACT_CONFINEMENT_COMMAND_IDS = (
    PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS - CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS
)


def _tracked_dirty_content_digests(project_root: str, status_stdout_z: str) -> dict[str, str]:
    """SHA-256 content digest of every TRACKED path `status_stdout_z`
    (porcelain v1, ``-z``) reports as touched in the index or worktree --
    i.e. every entry that is NEITHER untracked (``??``) NOR ignored (``!!``)
    (Issue #2199 OWNER feedback P2(b)).

    Closes a real gap in comparing `git status` TEXT alone: git's status
    LETTER for an ALREADY-dirty tracked file does not change merely because
    its content changes further (``M path`` both before and after a span,
    even though the bytes differ) -- this hashes those paths' actual
    current bytes so within-span content drift on an already-dirty path is
    still detected, instead of being silently absorbed into an
    unchanged-looking status line. Bounded by the number of ALREADY-dirty
    tracked paths at snapshot time (zero for any invocation starting from a
    clean primary checkout, the normal production case) -- never a walk of
    the whole tree, and never a second, independent full-repo scan.

    Mirrors ``_git_status_paths()``'s own rename-pair field consumption
    (a rename entry's second, bare NUL-terminated field is its ORIGINAL
    path, never itself a new status entry) so a rename is never
    misparsed into a bogus phantom path.
    """
    digests: dict[str, str] = {}
    fields = [field for field in status_stdout_z.split("\0") if field]
    i = 0
    while i < len(fields):
        field = fields[i]
        if len(field) < 4:
            i += 1
            continue
        xy = field[:2]
        rel_path = field[3:]
        is_rename = field[0] == "R" or field[1] == "R"
        i += 2 if (is_rename and i + 1 < len(fields)) else 1
        if "?" in xy or "!" in xy:
            continue
        full_path = Path(project_root) / rel_path
        try:
            if full_path.is_symlink() or not full_path.is_file():
                continue
            digests[rel_path] = hashlib.sha256(full_path.read_bytes()).hexdigest()
        except OSError:
            digests[rel_path] = "<unreadable>"
    return digests


def capture_primary_checkout_invariant_snapshot(project_root: str) -> dict[str, str]:
    """A ``--no-optional-locks``-guarded snapshot of the PRIMARY checkout's
    identity-bearing state (Issue #2199 AC1), used as an unconditional
    STOP-ON-ANY-OBSERVED-CHANGE safety net around a dedicated-worktree
    child dispatch (Issue #2199 OWNER feedback P2).

    What this DOES prove (compared before vs after a dispatch span, on
    both the success and the failure path): the primary checkout's
    toplevel path, HEAD ref/OID, `git status` text (including individual
    untracked-file enumeration -- see below), AND, for every path already
    tracked-dirty at snapshot time, that path's own content digest, are
    all identical. Combined, an already-dirty tracked file's content
    changing WITHOUT its status letter changing (`M path` before and
    after, different bytes) is caught by the digest, not just the text
    (Issue #2199 OWNER feedback P2(b) -- the status-text-only comparison
    this replaced could not distinguish that case, so "byte-identical" was
    not, in fact, always true of it).

    What this does NOT prove: that the DEDICATED CHILD ITSELF caused an
    observed difference, as opposed to any other concurrent process
    touching the SAME primary checkout during the same span (Issue #2199
    OWNER feedback P2(a)). This is a conservative, unconditional stop
    condition -- ANY observed difference in this checkout's state across
    the span fails closed, regardless of attribution -- not a targeted
    attribution/causality proof. A caller must not describe a failure here
    as "the dedicated dispatch mutated primary"; the accurate description
    is "primary's observable state did not remain unchanged across this
    span" (see `_emit_primary_checkout_drift_failure()`'s message).

    ``--untracked-files=all`` DOES enumerate individual untracked files
    (not merely each untracked top-level directory as a single entry) --
    this is INTENTIONAL, not an oversight: a new file appearing inside an
    already-untracked directory must also be detected for AC1's
    byte-identical proof, and `--untracked-files=normal` would silently
    miss it (the containing directory's own status entry would be
    identical before and after). This is still far cheaper than
    `_snapshot_repo_paths()`'s full recursive `os.walk()` +
    per-entry `lstat()` (a single `git status` subprocess call here, versus
    a Python-side filesystem walk over the entire tree there) -- but it is
    not free, and this docstring no longer claims it avoids per-file
    enumeration.
    """
    git = shutil.which("git") or "git"

    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [git, "--no-optional-locks", "-C", project_root, *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    toplevel = _run("rev-parse", "--show-toplevel")
    toplevel_raw = toplevel.stdout if toplevel.returncode == 0 else f"<error:{toplevel.returncode}>"
    symbolic = _run("symbolic-ref", "-q", "HEAD")
    head_mode_raw = f"branch:{symbolic.stdout.strip()}" if symbolic.returncode == 0 else "detached"
    head_oid = _run("rev-parse", "HEAD")
    head_oid_raw = head_oid.stdout if head_oid.returncode == 0 else f"<error:{head_oid.returncode}>"
    status = _run("status", "--porcelain=v1", "--untracked-files=all", "-z")
    status_raw = (
        status.stdout.replace("\0", "\\0")
        if status.returncode == 0
        else f"<error:{status.returncode}>"
    )
    tracked_dirty_content_digest_raw = (
        json.dumps(_tracked_dirty_content_digests(project_root, status.stdout), sort_keys=True)
        if status.returncode == 0
        else f"<error:{status.returncode}>"
    )
    return {
        "toplevel_raw": toplevel_raw,
        "head_mode_raw": head_mode_raw,
        "head_oid_raw": head_oid_raw,
        "status_raw": status_raw,
        "tracked_dirty_content_digest_raw": tracked_dirty_content_digest_raw,
    }


def _strict_ancestor_of_race_tolerant_root(rel_path: str) -> bool:
    """True when `rel_path` (a directory-status entry, e.g. `artifacts/`) is a
    strict ancestor of at least one race-tolerant-unattributable root, but is
    not itself one of those roots.

    Issue #1409 REQUEST_CHANGES (P1): Git's `--ignored=matching` collapses an
    entire ignored directory tree into a single status entry for the
    ignore-pattern-matched directory itself (e.g. `!! artifacts/`), not its
    descendants, whenever that ignored directory does not yet exist in the
    before-snapshot. Because the real repo's `.gitignore` ignores
    `artifacts/` as a whole, a cold-start creation of
    `artifacts/session-manifest-runtime/**` is folded and reported as the
    parent `artifacts/` entry rather than the excluded subtree -- this helper
    identifies that folding so the caller can expand it instead of
    fail-closing on the collapsed ancestor path.
    """
    normalized = rel_path.replace(os.sep, "/").rstrip("/")
    for root in _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS:
        if normalized != root and root.startswith(normalized + "/"):
            return True
    return False


def _expand_folded_ignored_status_dir(project_root: str, rel_dir: str) -> set[str]:
    """Expand a single Git-status-folded ignored-directory entry (e.g.
    `artifacts/`) into its actual leaf file paths via a *targeted*
    (path-restricted, not repo-wide) `--ignored=traditional` scan. Restricting
    the scan to `rel_dir` keeps this bounded and avoids reintroducing a
    repo-wide `--ignored=traditional` walk (explicitly rejected as an
    unbounded alternative in Issue #1409 REQUEST_CHANGES)."""
    git = shutil.which("git") or "git"
    out = subprocess.run(
        [
            git,
            "-C",
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=traditional",
            "-z",
            "--",
            rel_dir,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("git_status_failed")
    paths: set[str] = set()
    for field in (f for f in out.stdout.split("\0") if f):
        if len(field) < 4:
            continue
        paths.add(field[3:])
    return paths


def _is_real_nonsymlink_dir(project_root: str, rel_dir: str) -> bool:
    path = Path(project_root) / rel_dir.rstrip("/")
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    return not _is_symlink_path(path)


def _strict_ancestor_of_allowed_artifact_root(
    project_root: str, issue_number: str, rel_path: str, command_id: str
) -> bool:
    candidate = (Path(project_root) / rel_path.rstrip("/")).resolve()
    return any(
        candidate != allowed_root.resolve() and allowed_root.resolve().is_relative_to(candidate)
        for allowed_root in _allowed_artifact_roots(project_root, issue_number, command_id)
    )


def _expand_new_status_paths(
    project_root: str, new_raw_paths: set[str], issue_number: str = "", command_id: str = ""
) -> set[str]:
    """Expand any newly-appeared folded-ignored-ancestor entries (see
    `_strict_ancestor_of_race_tolerant_root`) into their real leaf paths so
    that race-tolerant-root exclusion can be applied precisely, instead of
    fail-closing on the collapsed ancestor directory itself.

    Safety (Issue #1409 REQUEST_CHANGES P1): expansion only happens when the
    collapsed entry is confirmed on disk to be a real, non-symlink directory.
    If the entry has instead been substituted by a file or a symlink (parent
    substitution), expansion is skipped and the raw entry is kept as-is so it
    fails closed via the normal unauthorized-path path.
    """
    expanded: set[str] = set()
    for path in new_raw_paths:
        if (
            path.endswith("/")
            and (
                _strict_ancestor_of_race_tolerant_root(path)
                or _strict_ancestor_of_allowed_artifact_root(
                    project_root, issue_number, path, command_id
                )
            )
            and _is_real_nonsymlink_dir(project_root, path)
        ):
            expanded.update(_expand_folded_ignored_status_dir(project_root, path))
            continue
        expanded.add(path)
    return expanded


def _snapshot_repo_paths(
    project_root: str, issue_number: str, command_id: str = ""
) -> dict[str, tuple[str, int, int]]:
    root = Path(project_root)
    allowed_roots = _allowed_artifact_roots(project_root, issue_number, command_id)
    peer_roots = _race_tolerant_unattributable_roots(project_root)
    allowed_parent_dirs: set[Path] = set()
    for allowed_root in allowed_roots:
        for parent in allowed_root.parents:
            allowed_parent_dirs.add(parent)
            if parent == root:
                break
    if command_id in CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS:
        # The existing edit-issue transaction uses the repository-approved
        # transaction-local ``tmp/`` workspace for its candidate and input
        # files, and deletes those files before returning.  Ignore only the
        # directory-node mtime churn here; any residual child path remains in
        # the snapshot and is still rejected below.
        allowed_parent_dirs.add(root / "tmp")
    # Issue #2584 In Scope item 5: a SEPARATE, independent gate for the SAME
    # `tmp/` directory-node mtime-churn exclusion, covering the 3 additional
    # command_ids whose dispatch also reaches `edit_issue_txn.py`'s
    # transaction core outside the `CONTRACT_UPDATE_MUTATION_DEDICATED_
    # COMMAND_IDS` branch above. `allowed_parent_dirs` is a set, so adding
    # the SAME `root / "tmp"` path here when BOTH conditions happen to be
    # true (e.g. `contract_update.run.with_anchor`) is a no-op, never a
    # double-exemption -- only the directory-NODE's own entry is ever
    # skipped; every residual child path left inside `tmp/` after the child
    # returns is still walked and still rejected below, exactly as before.
    if command_id in TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS:
        allowed_parent_dirs.add(root / "tmp")
    # Issue #1409: also skip recording the directory-node entry (its own
    # mtime/size) for every ancestor of each race-tolerant-unattributable
    # root. Without this, a *new* top-level ancestor directory (e.g.
    # `artifacts/`, when it does not yet exist before the child command
    # runs and is first created by a peer/hook write under
    # `artifacts/session-manifest-runtime/**`) would itself appear as a
    # brand-new snapshot entry and be misreported as an unauthorized write,
    # even though the pruning above already fully excludes the peer root's
    # own contents. `.claude/worktrees` and
    # `.claude/artifacts/issue-refinement-loop` never hit this gap because
    # their ancestor (`.claude`) already coincides with an ancestor of this
    # issue's own `allowed_root`; `artifacts/session-manifest-runtime`'s
    # ancestor (`artifacts`) does not share that coincidence, so it needs
    # its own explicit ancestor-skip set.
    for peer_root in peer_roots:
        for parent in peer_root.parents:
            allowed_parent_dirs.add(parent)
            if parent == root:
                break

    snapshot: dict[str, tuple[str, int, int]] = {}
    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        if current_path == root / ".git":
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if (current_path / name) != root / ".git"]
        # Prune volatile peer-session roots entirely so that concurrent
        # local sessions/agents writing under them are never walked into
        # (and therefore never contribute snapshot drift for this command).
        dirnames[:] = [name for name in dirnames if (current_path / name) not in peer_roots]
        # PR #2364 review P1-1: also prune a genuine repo-root-only pytest
        # cold-start temp dir (`pytest-cache-files-<random>`, see
        # `_is_pytest_cache_cold_start_temp_path` above) from this full
        # filesystem walk. `peer_roots` above only prunes exact, statically
        # known paths (like `.pytest_cache` itself); this dynamic-suffix
        # sibling needs its own prefix check, scoped to `current_path ==
        # root` so a nested lookalike is never pruned.
        if current_path == root:
            dirnames[:] = [
                name for name in dirnames if not _is_pytest_cache_cold_start_temp_path(name)
            ]
        for name in ["."] + dirnames + filenames:
            path = current_path if name == "." else current_path / name
            if path == root / ".git":
                continue
            if path in peer_roots:
                continue
            if any(
                path == allowed_root or path.is_relative_to(allowed_root)
                for allowed_root in allowed_roots
            ):
                continue
            if path in allowed_parent_dirs:
                continue
            try:
                stat = path.lstat()
            except FileNotFoundError:
                continue
            rel = os.path.relpath(path, root)
            snapshot[rel] = (
                "dir" if path.is_dir() else "file",
                stat.st_mtime_ns,
                stat.st_size,
            )
    return snapshot


def _ensure_artifact_path_safe(project_root: str, issue_number: str, command_id: str = "") -> Path:
    artifact_roots = _allowed_artifact_roots(project_root, issue_number, command_id)
    for artifact_root in artifact_roots:
        parent = artifact_root.parent
        for candidate in (parent, *parent.parents):
            if candidate == Path(project_root).parent:
                break
            if candidate.exists() and _is_symlink_path(candidate):
                raise RuntimeError("artifact_parent_symlink_not_allowed")
        if artifact_root.exists() and (
            _is_symlink_path(artifact_root) or artifact_root.is_symlink()
        ):
            raise RuntimeError("artifact_root_symlink_not_allowed")
    return artifact_roots[0]


# `pyproject.toml`'s `[tool.uv].required-version` is the canonical, read-only
# SSOT for the repository's pinned `uv` version (Issue #1598). This module
# never writes it and never introduces a second version literal -- it only
# consumes it as a defense-in-depth "version 照合" check on a `uv`
# resolution that did not come from the strongly-validated hostedtoolcache
# trust root (e.g. a system PATH directory such as `/usr/local/bin`); see
# `_validate_trusted_executable_version` below (Issue #2251 AC7).
_EXACT_VERSION_PIN_RE = re.compile(r"^==(?P<version>\d+\.\d+\.\d+)$")


def _is_managed_uv_project(root: str) -> bool:
    """True unless `pyproject.toml`'s `[tool.uv].managed` is explicitly
    `false` (Issue #2199 OWNER feedback P1-1 fix_delta collateral).

    An unmanaged project never triggers `uv`'s own environment sync/`.venv`
    creation at all -- `uv sync`/`uv run` refuse to manage its environment
    by design (`error: The project is marked as unmanaged`) -- so it was
    never exposed to the P1-1 false-positive `unauthorized_write_path`
    misclassification in the first place, and
    `ensure_dedicated_execution_environment_ready`'s explicit preparation
    step has nothing to prepare for it. Treating a read/parse failure as
    "managed" (the fail-closed default, matching every real production
    `pyproject.toml`, which never sets `managed = false`) keeps this check
    itself from becoming a NEW way to silently skip the P1-1 fix.
    """
    pyproject_path = Path(root) / "pyproject.toml"
    try:
        raw_toml = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return True
    try:
        data = tomllib.loads(raw_toml)
    except tomllib.TOMLDecodeError:
        return True
    uv_section = data.get("tool", {}).get("uv")
    if not isinstance(uv_section, dict):
        return True
    return uv_section.get("managed", True) is not False


def _required_uv_version(project_root: str) -> str | None:
    """Return the numeric `uv` version pinned by `pyproject.toml`'s
    `[tool.uv].required-version`, or None if that SSOT is missing,
    unreadable, or not an exact `==X.Y.Z` pin (Issue #2251 AC7)."""
    pyproject_path = Path(project_root) / "pyproject.toml"
    try:
        raw_toml = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = tomllib.loads(raw_toml)
    except tomllib.TOMLDecodeError:
        return None
    uv_section = data.get("tool", {}).get("uv")
    if not isinstance(uv_section, dict):
        return None
    raw_pin = uv_section.get("required-version")
    if not isinstance(raw_pin, str):
        return None
    match = _EXACT_VERSION_PIN_RE.fullmatch(raw_pin.strip())
    return match.group("version") if match else None


def _validate_trusted_executable_version(name: str, resolved: str, project_root: str) -> bool:
    """Defense-in-depth "version 照合" for a trusted executable resolved from
    outside the strongly-validated hostedtoolcache trust root (Issue #2241 /
    PR #2247 review P1-3; Issue #2251 narrowed the trust root itself by
    excluding account-home `~/.local/bin`, but a `uv` resolved from a
    remaining non-hostedtoolcache entry -- e.g. `/usr/local/bin` -- still
    gets this check).

    This is not a trust boundary on its own (an executable that emits a
    matching `--version` banner is not thereby proven authentic), but it
    does reject an executable that was replaced with something that does
    not even claim to be the pinned `uv` version. For `uv`, the expected
    version is the exact `pyproject.toml` [tool.uv].required-version pin
    (Issue #2251 AC7) -- if that SSOT is missing/malformed there is nothing
    trustworthy to compare against, so this fails closed (returns False)
    rather than skipping the check."""
    if name != "uv":
        # No registered expectation for this executable name -- nothing to
        # compare against, so this check is inapplicable rather than a
        # rejection (the ownership/commonpath/regular-file checks above
        # remain the primary defense for non-`uv` names).
        return True
    required_version = _required_uv_version(project_root)
    if required_version is None:
        return False
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    match = re.match(r"^uv (\d+\.\d+\.\d+)", (proc.stdout or "").strip())
    return bool(match) and match.group(1) == required_version


def _os_account_home() -> str | None:
    """Return the real OS account home directory for the UID this process
    itself runs as, resolved via the passwd database (`pwd.getpwuid`) --
    never via the ambient `HOME` environment variable, which a caller or
    launcher can freely set to an arbitrary path (e.g. `HOME=/tmp/evil`)
    without that changing which Unix account this process actually runs
    as (Issue #2276 decision record). Returns None if the account has no
    passwd entry, so callers fail closed rather than raising."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return None


def _trusted_account_home_uv_dir() -> list[str]:
    """Return the real-account-home `.local/bin` directory as a `uv`
    PATH candidate (Issue #2276 / #2280), or an empty list if unavailable.

    This is the standard install location used by the official `uv`
    standalone installer on Linux, so no bespoke `trusted-bin` directory,
    sudo installation step, or repository-owned checksum authority is
    introduced. The directory is derived from `_os_account_home()`, not
    the ambient `HOME` env var, so an attacker who can only set `HOME`
    cannot relocate this trust candidate. It is not, on its own, a
    stronger guarantee than the pre-existing system PATH lane: anything
    resolved from here still must pass the exact-version check in
    `_validate_trusted_executable_version` (this directory is never part
    of the hostedtoolcache trust root), so a wrong-version or replaced
    `uv` here still fails closed exactly like `/usr/local/bin` does
    today."""
    home = _os_account_home()
    if not home:
        return []
    candidate = os.path.join(home, ".local", "bin")
    return [candidate] if os.path.isdir(candidate) else []


def _safe_path_entries() -> list[str]:
    """Return the ordered list of trusted, name-agnostic PATH directories
    consumed by `_resolve_trusted_executable`/`_sanitize_env`/
    `sanitized_git_subprocess_env` for every trusted executable name this
    module resolves (`uv`, `git`, `ssh`, ...).

    Real-account-home `.local/bin` (Issue #2276 / #2280 decision record) is
    deliberately NOT included here: see `_resolve_trusted_executable`,
    which adds it as a `uv`-only candidate on top of this shared list. This
    list is shared across every executable name, and a real account's
    `~/.local/bin` commonly contains a whole toolchain -- `git` included --
    installed by that same account, unlike hostedtoolcache (which
    structurally can only ever contain the pinned `uv`). Since
    `_validate_trusted_executable_version` is a no-op for names other than
    `uv`, widening this shared list to include account-home would let a
    same-UID attacker's lookalike `git` (or any other non-`uv` name
    resolved through this module) be trusted with zero version-pin defense
    (Issue #2280 Out of Scope: this module's `uv` trust boundary decision
    must not widen trust for any other executable name).

    This module does not claim to contain an attacker who already has
    arbitrary code execution as the same Unix account this process runs
    as -- such an attacker could modify any of these directories (or this
    module's own source) regardless of which ones are listed here; see
    `docs/dev/workflow.md` "Not Controlled". What this list *does* defend
    is "unintended executable selection": ambient `PATH` pollution and
    CWD/project-local substitution are excluded by construction. No new
    dedicated `trusted-bin` directory, sudo installation step, or
    repository-owned checksum authority is introduced by this lane.
    """
    return _dedupe_path_entries([*_trusted_toolchain_dirs("uv"), *_SYSTEM_STANDARD_PATH_DIRS])


# Fixed system standard directories, shared by `_safe_path_entries()` and
# `_resolve_trusted_executable`'s `uv`-only account-home extension below.
_SYSTEM_STANDARD_PATH_DIRS: tuple[str, ...] = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)


def _dedupe_path_entries(entries: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return ordered


# Fixed, hardcoded hosted-toolcache roots per trusted executable name. Not
# sourced from any environment variable (e.g. `UV_INSTALL_DIR`) -- a child
# process must never be able to widen its own trusted PATH by pointing an
# env var at an attacker-controlled directory (Issue #2241 rejected
# workaround list).
_TRUSTED_TOOLCHAIN_HOSTED_ROOTS: dict[str, Path] = {
    "uv": Path("/opt/hostedtoolcache/uv"),
}

# A trusted hostedtoolcache version-directory component must look like a
# version string (e.g. "0.4.30", "3.12.4"). This is a cheap defense-in-depth
# structural check ("version 照合") on top of the realpath/commonpath/
# ownership checks below -- it rejects directory names that were tampered
# with to smuggle something other than an actual toolchain version through
# the trust boundary.
_TOOLCHAIN_VERSION_DIR_RE = re.compile(r"^\d+(\.\d+){1,3}([+.\-][0-9A-Za-z.]+)?$")


def _trusted_toolchain_dirs(executable_name: str) -> list[str]:
    """Return trust-validated hostedtoolcache directories that may contain
    `executable_name`.

    Generalized from the former `uv`-only `_trusted_uv_toolcache_dirs`
    (Issue #2241) into a lookup keyed by `executable_name`, so future
    trusted toolchains only need an entry in
    `_TRUSTED_TOOLCHAIN_HOSTED_ROOTS` rather than a new bespoke resolver
    function.

    Each candidate is validated by:
      - regular-file type verification on the realpath-resolved target
        (rejects directories, FIFOs, sockets, devices, and symlinks that
        resolve to a non-regular file)
      - commonpath containment of the realpath-resolved target under the
        fixed trust root (rejects a symlink/copy pointing outside the
        hostedtoolcache root)
      - a version-shaped directory component ("version 照合")
      - ownership(uid) verification: the resolved regular file must be
        owned by root (uid 0 -- hostedtoolcache is root-installed in CI
        runner images) or by the account this process itself runs as
    """
    root = _TRUSTED_TOOLCHAIN_HOSTED_ROOTS.get(executable_name)
    if root is None or not root.is_dir():
        return []

    trusted_dirs: list[str] = []
    root_real = os.path.realpath(root)
    account_uid = os.getuid()
    for candidate in sorted(root.glob("*/x86_64")):
        version_component = candidate.parent.name
        if not _TOOLCHAIN_VERSION_DIR_RE.match(version_component):
            continue
        exe_path = candidate / executable_name
        if not exe_path.is_file() or not os.access(exe_path, os.X_OK):
            continue
        real = os.path.realpath(exe_path)
        try:
            real_st = os.stat(real)
        except OSError:
            continue
        if not stat.S_ISREG(real_st.st_mode):
            continue
        if real_st.st_uid not in (0, account_uid):
            continue
        if os.path.commonpath([root_real, real]) != root_real:
            continue
        trusted_dirs.append(str(candidate))
    return trusted_dirs


def _resolve_trusted_executable(name: str, project_root: str) -> str:
    """Resolve `name` to a trust-validated executable path.

    For "python3", the trust boundary is validated against the fully
    resolved (symlink-following) target, but the *returned* path preserves
    venv identity (`sys.executable` itself, unresolved) instead of the
    realpath. Returning the realpath here previously caused `uv run
    <realpath>` to lose association with the project venv whenever
    `sys.executable` was a symlink into a bare interpreter (e.g. a
    `uv python install`-managed toolchain with no project dependencies of
    its own), which made child subprocesses unable to import project
    dependencies (Issue #2073 root cause: jsonschema unimportable in CI).

    Check/use window (Issue #2073 human-review P1-2): returning the
    unresolved `sys.executable` path instead of its realpath means the
    string validated here (via `real`, below) and the string ultimately
    invoked by the caller are not byte-identical -- if something rewrote
    the `.venv/bin/python3` symlink between this call returning and the
    caller spawning the child process, the caller would exec whatever the
    symlink points to *at spawn time*, not the target validated here. This
    is a real TOCTOU window in the classic sense (CWE-367), but it cannot
    be closed by executing through an already-opened file descriptor
    (e.g. `/proc/self/fd/N` or `fexecve`) the way this module's other
    symlink-race guards do for *file reads*: CPython's own venv detection
    (`pyvenv.cfg` discovery, which determines `sys.prefix` and therefore
    which `site-packages` a spawned interpreter uses) walks *upward from
    the directory of the path it was invoked with*, so the invoked path
    string itself must remain `.../.venv/bin/python3` at exec time for the
    child to see the project's dependencies -- executing via a captured fd
    (which has no adjacent `pyvenv.cfg`) would silently reproduce the exact
    bug this function exists to fix. Given that, this function's only
    caller (`_resolve_child_argv`) re-resolves and re-validates on every
    single invocation immediately before the child is spawned (no caching,
    no reuse of a previously-resolved value across calls -- see
    `test_ac10_resolve_trusted_executable_is_not_cached_across_calls` in
    `tests/test_skill_runtime_preflight_bytecode_cache.py`), which bounds
    the window to the handful of Python statements between this function
    returning and `subprocess`/`Popen` being invoked, with no filesystem or
    network I/O in between. An attacker who can rewrite `.venv/bin/python3`
    inside that window already has write access to the repository working
    tree the executor itself operates in, which is a strictly stronger
    position than this check defends against elsewhere in this module.

    Pre-existing (not introduced by this fix) characteristic of the trust
    check itself, found while writing the regression test above: for
    `name == "python3"`, `runtime_dir` below is computed from `sys.executable`
    -- the exact same value being validated -- so `real_parent == runtime_dir`
    holds tautologically regardless of what the symlink resolves to, and the
    `{name}_outside_trusted_path` branch can never fire for `python3` (it is
    a real, load-bearing check only for `name == "uv"`, where `resolved` is
    independently looked up via `shutil.which`). The only check that
    actually constrains where `python3` may resolve is `{name}_inside_project_root`,
    below. This was true before this fix too (the pre-fix code computed
    `real`/`real_parent`/`runtime_dir` identically, all derived from
    `sys.executable`); this fix does not change which check is effective,
    only which string is returned once that check passes.
    """
    # The account-home `.local/bin` lane (Issue #2276 / #2280) is added on
    # top of the shared, name-agnostic `_safe_path_entries()` list ONLY for
    # `uv` -- never for `git`, `ssh`, or any other name resolved through
    # this function -- so that lane does not widen trust for executables
    # whose resolution has no version-pin defense (see `_safe_path_entries`
    # docstring). `_sanitize_env`/`sanitized_git_subprocess_env` also call
    # `_safe_path_entries()` directly (without this addition), so the
    # account-home lane never reaches the generic child `PATH` either.
    # Ordering: hostedtoolcache first (strongest validation), then the
    # account-home lane, then system standard directories -- so a
    # correctly pinned hostedtoolcache `uv` is preferred over the
    # local-dev convenience lane whenever both are present.
    if name == "uv":
        safe_entries = _dedupe_path_entries(
            [
                *_trusted_toolchain_dirs("uv"),
                *_trusted_account_home_uv_dir(),
                *_SYSTEM_STANDARD_PATH_DIRS,
            ]
        )
    else:
        safe_entries = _safe_path_entries()
    safe_path = os.pathsep.join(safe_entries)
    if name == "python3":
        resolved = sys.executable
    else:
        resolved = shutil.which(name, path=safe_path)
    if not resolved:
        if name == "uv":
            # Issue #2275: `uv_not_found` gets a structured diagnostic payload
            # instead of the bare `{name}_not_found` string used by every
            # other trusted executable name. `candidates_searched` is the
            # exact `safe_entries` list already built above for this `uv`
            # branch (never recomputed via `_safe_path_entries()`), so the
            # reported candidates are byte-identical to what was actually
            # passed to `shutil.which(name, path=safe_path)` -- no ambient
            # `PATH` entries and no account-home lane leaked in from the
            # shared, name-agnostic helper. `recommended_install_dir` is
            # derived from the real OS account home (`_os_account_home()`,
            # itself `pwd.getpwuid`-backed, never the ambient `HOME` env
            # var) and is reported even when that directory does not yet
            # exist -- unlike `candidates_searched`, which only ever
            # contains directories this process actually searched.
            account_home = _os_account_home()
            recommended_install_dir = (
                os.path.join(account_home, ".local", "bin") if account_home else None
            )
            diagnostic_payload = {
                "error": "uv_not_found",
                "candidates_searched": list(safe_entries),
                "expected_version": _required_uv_version(project_root),
                "recommended_install_dir": recommended_install_dir,
                "remediation_hint": (
                    "Install the pinned uv version with the official "
                    "standalone installer, specifying UV_INSTALL_DIR="
                    f"{recommended_install_dir or '<account-home>/.local/bin'} "
                    "so it lands in the no-sudo user-local lane, then "
                    "verify with `uv --version` for an exact match "
                    "against pyproject.toml's [tool.uv].required-version. "
                    "See docs/dev/workflow.md "
                    "'### Trusted uv のローカル開発復旧'."
                ),
            }
            raise RuntimeError("uv_not_found: " + json.dumps(diagnostic_payload))
        raise RuntimeError(f"{name}_not_found")
    real = os.path.realpath(resolved)
    project_root_real = os.path.realpath(project_root)
    if os.path.commonpath([project_root_real, real]) == project_root_real:
        raise RuntimeError(f"{name}_inside_project_root")
    allowed_dirs = {os.path.realpath(entry) for entry in safe_entries}
    real_parent = os.path.realpath(os.path.dirname(real))
    runtime_dir = os.path.realpath(str(Path(sys.executable).resolve().parent))
    if real_parent not in allowed_dirs and real_parent != runtime_dir:
        raise RuntimeError(f"{name}_outside_trusted_path")
    if name != "python3":
        # Issue #2241 / PR #2247 review P1-3(c) / Issue #2251 / Issue #2276:
        # a resolution that came from outside the strongly-validated
        # hostedtoolcache trust root (a system PATH directory such as
        # `/usr/local/bin`, or the real-account-home `.local/bin` lane
        # re-permitted by Issue #2276) gets an additional "version 照合"
        # defense-in-depth check -- see `_validate_trusted_executable_version`
        # docstring for why this is a confirmation, not a trust boundary, on
        # its own.
        hosted_dirs = {os.path.realpath(entry) for entry in _trusted_toolchain_dirs(name)}
        if real_parent not in hosted_dirs and not _validate_trusted_executable_version(
            name, real, project_root
        ):
            raise RuntimeError(f"{name}_version_mismatch")
    return resolved if name == "python3" else real


def _detect_legacy_spark_execution_request(command_id: str) -> tuple[str | None, str | None] | None:
    """Issue #2651 OWNER review fix_delta
    (source repository pull 2662 review
    P1 blocker 1): detect a legacy Spark execution request on the
    canonical `preflight.run` executor's OWN input boundary, before
    `_sanitize_env()` ever strips `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK`
    from the child environment.

    Stripping those two env vars before dispatch (as `_sanitize_env()`
    already does below) is not by itself a rejection -- it only prevents
    the retired directive from reaching a CHILD process. Without this
    check, `workflow_start_entry.py`'s own retired-directive check never
    even runs for a `preflight.run`-routed request, because by the time
    that check would see `spark_mode`, this executor has already replaced
    it with `None` -- indistinguishable from "Spark not requested at all"
    -- and the ordinary producer/inner-preflight flow proceeds unchanged
    (exactly the silent-fallthrough OWNER flagged).

    Only `command_id == "preflight.run"` is in scope: it is the sole
    command id this allowlist ever carried these two env vars for (see
    `_sanitize_env()` below), so it is the only one where stripping them
    could have silently converted a real request into `spark_mode=None`.
    A direct, non-canonical-executor invocation of
    `workflow_start_entry.py` never passes through this executor at all,
    so it is unaffected and keeps its own existing retired-rejection path.

    Returns the raw `(LOOP_SPARK_MODE, LOOP_SPARK_FALLBACK)` values when
    either is present in `os.environ` for `command_id == "preflight.run"`,
    else `None` (no legacy request detected; ordinary `preflight.run`
    dispatch is unaffected)."""
    if command_id != "preflight.run":
        return None
    mode = os.environ.get("LOOP_SPARK_MODE")
    fallback = os.environ.get("LOOP_SPARK_FALLBACK")
    if mode is None and fallback is None:
        return None
    return (mode, fallback)


def _emit_legacy_spark_retired_rejection(
    issue_number: int, mode: str | None, fallback: str | None
) -> int:
    """Deterministic, stateless retired rejection for a legacy Spark
    execution request detected by `_detect_legacy_spark_execution_request`.
    Never spawns a child/inner preflight process, never substitutes a
    different model or Agent for the request, and never re-adds
    `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK` to any child environment."""
    print(
        json.dumps(
            {
                "schema": "SKILL_RUNTIME_EXEC_RESULT_V1",
                "status": "blocked",
                "issue_number": issue_number,
                "command_id": "preflight.run",
                "reason": "spark_delegation_retired",
                "detail": (
                    "GPT-5.3-Codex-Spark delegation is retired in this repository "
                    "(Issue #2651). Unset LOOP_SPARK_MODE/LOOP_SPARK_FALLBACK and "
                    "re-run; this request is not silently substituted with another "
                    "model or Agent. No child or inner preflight process was started."
                ),
                "requested_spark_mode": mode,
                "requested_spark_fallback": fallback,
            }
        )
    )
    return 3


def _sanitize_env(project_root: str, command_id: str = "") -> dict[str, str]:
    allowed_keys = {
        "GH_HOST",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
    # `GH_CONFIG_DIR` is an invocation-scoped GitHub CLI configuration path.
    # Preserve only the finite command IDs whose actual child path reaches a
    # GitHub CLI consumer. Do not infer a value when absent, inspect
    # configuration contents, or widen this to prefixes, all mutation
    # commands, or all human-context profiles.
    gh_config_dir_carrier_command_ids = frozenset(
        {
            "preflight.run",
            # Issue #2872: the production anchor/human-context/agent-report
            # preflight profiles' actual child (`run_refinement_preflight.py`)
            # reads Issue/comments/anchor through native `gh`, so a stored
            # `gh auth login` configuration (reached only via the launcher's
            # fixed `GH_CONFIG_DIR`) must reach it the same way bare
            # `preflight.run` already does (#2403). The fixture lane
            # (`preflight.run.fixture.*`) never invokes `gh` and stays out.
            "preflight.run.with_anchor",
            "preflight.run.with_human_context",
            "preflight.run.with_agent_report",
            "contract_update.run.with_anchor",
            "contract_update.run.with_human_context",
            "repair_action.apply",
            "structural_repair_action.apply",
            # PR #2694 review fix_delta (P0-1): `owner_reaction.decide` is a
            # real `gh api` consumer (its first hop, owner_reaction_decision.py,
            # shells out to `gh` for the production profile). Without this,
            # a canonical Claude-GPT environment authenticated via a normal
            # `gh auth login` stored credential (no explicit GH_TOKEN/
            # GITHUB_TOKEN) could never dispatch it. `owner_reaction.decide.
            # fixture` (the local-only `--gh-fixture-file` lane, which never
            # invokes `gh` at all) is deliberately NOT added here.
            "owner_reaction.decide",
        }
    )
    if command_id in gh_config_dir_carrier_command_ids:
        allowed_keys.add("GH_CONFIG_DIR")

    # Only bare `preflight.run`'s first hop (`workflow_start_entry.py`) reads
    # the invocation-scoped capability request. Keep this policy separate
    # from the GitHub credential carrier policy above.
    #
    # Issue #2651: `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK` are no longer
    # carried through this allowlist for any command id. GPT-5.3-Codex-Spark
    # delegation has been retired; a caller that still sets these env vars
    # for a direct (non-canonical-executor) `workflow_start_entry.py`
    # invocation still gets a deterministic retired rejection from that
    # module's own `run()`. For `preflight.run` specifically,
    # `main()`'s `_detect_legacy_spark_execution_request()` check (OWNER
    # review fix_delta, PR #2662#issuecomment-5736035898 blocker 1) already
    # returns a retired rejection BEFORE this function is ever reached when
    # either env var is set -- so this allowlist boundary only ever runs
    # for `preflight.run` once no legacy Spark request is present, and
    # simply never forwards them regardless.
    if command_id == "preflight.run":
        allowed_keys |= {
            "LOOP_PLANNED_OPERATIONS_JSON",
        }
    env = {
        key: value
        for key, value in os.environ.items()
        if value and (key in allowed_keys or key.startswith("SKILL_RUNTIME_TEST_"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CLAUDE_PROJECT_DIR"] = project_root
    env["PATH"] = os.pathsep.join(_safe_path_entries())
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def _validate_runtime_context(project_root: str, args: argparse.Namespace) -> Path:
    if os.path.realpath(os.getcwd()) != os.path.realpath(project_root):
        raise RuntimeError("cwd_not_canonical_main_root")
    branch = current_branch(project_root)
    default_branch = resolve_default_branch(project_root)
    if branch != default_branch:
        raise RuntimeError("root_not_default_branch")
    repo_slug = resolve_repo_slug(project_root)
    if not is_trusted_repo_slug(repo_slug) or args.repo != repo_slug:
        raise RuntimeError("repo_binding_mismatch")
    parsed = ExactSkillRuntimeCommand(
        command_id=args.command_id,
        issue_number=str(args.issue_number),
        repo=args.repo,
        argv=(),
    )
    if not command_allows_root_no_worktree(parsed):
        active_issue, entry = resolve_active_issue(project_root, project_root)
        if active_issue != str(args.issue_number):
            raise RuntimeError("active_issue_mismatch")
        if entry is None:
            raise RuntimeError("active_issue_worktree_missing")
    return _ensure_artifact_path_safe(project_root, str(args.issue_number), args.command_id)


def _resolve_child_argv(child_argv: Iterable[str]) -> list[str]:
    resolved = list(child_argv)
    if resolved[:3] == ["uv", "run", "python3"]:
        project_root = resolve_project_root()
        resolved[0] = _resolve_trusted_executable("uv", project_root)
        resolved[2] = _resolve_trusted_executable("python3", project_root)
    return resolved


# Issue #2161 (native Codex CLI retirement): the ledger content-transition
# validators that lived here (_read_bytes_or_none / _parse_ledger_bytes /
# _load_ledger_json / _is_valid_ledger_schema /
# _is_authorized_ledger_content_transition) were already dead code before
# this Issue -- Issue #1830 made launch-ledger state advisory-only, and
# nothing in this module called them. Removed as part of the DELETE_CHAIN.


def _find_unauthorized_repo_changes(
    project_root: str,
    issue_number: str,
    before_snapshot: dict[str, tuple[str, int, int]],
    before_status: set[str],
    command_id: str = "",
) -> str | None:
    # Issue #2161 (native Codex CLI retirement): the launch-ledger typed
    # exact-path exclusion (Issue #1502/#1830) was removed from this
    # function's diff filtering as part of the DELETE_CHAIN -- its sole live
    # producer (the former native Codex-agents parity checker script under
    # scripts/) no longer exists.
    after_snapshot = _snapshot_repo_paths(project_root, issue_number, command_id)
    after_status = _git_status_paths(project_root)

    new_raw_status_paths = after_status - before_status
    # Issue #1409 REQUEST_CHANGES (P1): expand any collapsed ignored-ancestor
    # directory entries (e.g. `!! artifacts/`) into their real leaf paths
    # before applying race-tolerant-root exclusion, so cold-start creation of
    # a race-tolerant subtree under an ignored parent is not misreported as
    # an unauthorized write to the collapsed parent itself.
    expanded_new_status_paths = _expand_new_status_paths(
        project_root, new_raw_status_paths, issue_number, command_id
    )
    new_status_paths = {
        path
        for path in expanded_new_status_paths
        if not _is_under_allowed_artifact_root(project_root, issue_number, path, command_id)
        and not _is_race_tolerant_unattributable_path(path)
        and not _is_pytest_cache_cold_start_temp_path(path)
    }
    if new_status_paths:
        return max(new_status_paths, key=lambda item: (len(Path(item).parts), item))
    if before_snapshot != after_snapshot:
        # Issue #1502 REQUEST_CHANGES (Blocker 4): the previous
        # implementation computed the symmetric-difference (create/delete) set
        # first and *skipped* the metadata-changed-for-existing-paths
        # computation whenever that symmetric difference was non-empty. That
        # meant a create/delete happening in the same invocation as an
        # existing sibling's *content* update (same path, different
        # mtime/size) would silently drop the sibling update from the diff.
        # Always compute the union of both: paths that appeared/disappeared,
        # and paths that exist on both sides but whose snapshot value
        # differs.
        before_paths = set(before_snapshot)
        after_paths = set(after_snapshot)
        changed = sorted(
            (before_paths ^ after_paths)
            | {
                path
                for path in before_paths & after_paths
                if before_snapshot[path] != after_snapshot[path]
            }
        )
        if changed:
            return max(changed, key=lambda item: (len(Path(item).parts), item))
        return None
    return None


def _repo_relative_path(project_root: str, path: str | Path) -> str:
    resolved = os.path.realpath(path)
    root_real = os.path.realpath(project_root)
    try:
        if os.path.commonpath([root_real, resolved]) == root_real:
            return os.path.relpath(resolved, root_real)
    except ValueError:
        pass
    return resolved


def _normalize_and_validate_runtime_env(project_root: str) -> list[tuple[str, str]]:
    worktrees_root = os.path.realpath(Path(project_root) / ".claude" / "worktrees")
    stale_entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for env_name in (
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
    ):
        env_value = os.environ.get(env_name)
        if not env_value:
            continue
        resolved = os.path.realpath(env_value)
        try:
            if os.path.commonpath([worktrees_root, resolved]) != worktrees_root:
                continue
        except ValueError:
            continue
        item = (env_name, _repo_relative_path(project_root, resolved))
        if item not in seen:
            seen.add(item)
            stale_entries.append(item)
    return stale_entries


def _parse_artifact_projection(stdout: str) -> list[str]:
    artifacts: list[str] = []
    collecting = False
    for line in stdout.splitlines():
        if line == "ARTIFACT:":
            collecting = True
            continue
        if not collecting:
            continue
        if not line.startswith("  "):
            break
        match = re.match(r"^\s{2}[^:]+:\s+(.+)$", line)
        if match:
            artifacts.append(match.group(1).strip())
    return artifacts


def _validate_stdout_artifact_projection(
    project_root: str, issue_number: str, stdout: str, command_id: str = ""
) -> list[str]:
    failures: list[str] = []
    root_real = os.path.realpath(project_root)
    for raw_path in _parse_artifact_projection(stdout):
        resolved = (
            os.path.realpath(raw_path)
            if os.path.isabs(raw_path)
            else os.path.realpath(Path(project_root) / raw_path)
        )
        rel_path = (
            os.path.relpath(resolved, root_real)
            if os.path.commonpath([root_real, resolved]) == root_real
            else resolved
        )
        if not _is_under_allowed_artifact_root(project_root, issue_number, rel_path, command_id):
            failures.append(_repo_relative_path(project_root, resolved))
    return failures


# ---------------------------------------------------------------------------
# Issue #2200: artifact confinement bounds + bounded dedicated-artifact
# cleanup, layered on top of the existing #2199/#2393 producer/consumer
# transport (`_validate_stdout_artifact_projection()` above, and the FD-based
# secure reader `run_refinement_preflight.secure_read_repair_apply_artifact()`
# on the consumer side -- neither of those is redesigned by this Issue).
#
# Scope: applies ONLY to `ARTIFACT_CONFINEMENT_COMMAND_IDS` (the 4 production
# preflight profiles). The 2 contract_update mutation profiles keep ONLY
# their existing PR #2520/#2533 inner-transaction-result-preservation
# contract, untouched by any function below.
# ---------------------------------------------------------------------------

# Fixed, non-caller-configurable bounds (Issue #2200 AC4). No new registry
# field, no environment-variable override -- a single literal ceiling shared
# by every production preflight profile's confinement check.
ARTIFACT_CONFINEMENT_MAX_FILE_BYTES = 4 * 1024 * 1024
ARTIFACT_CONFINEMENT_MAX_TOTAL_BYTES = 16 * 1024 * 1024
ARTIFACT_CONFINEMENT_MAX_COUNT = 16

# PR #2557 review fix-delta (P2-4): a fixed, non-caller-configurable
# directory-scan budget for `_bounded_cleanup_stale_artifact_leftovers()`'s
# `os.scandir()` enumeration -- bounds the SCAN itself (not just the
# post-filter candidate count already capped by `ARTIFACT_CONFINEMENT_MAX_COUNT`
# above), so an allowed artifact root containing an unbounded number of
# non-matching entries can never make the scan itself unbounded cost.
_LEFTOVER_SCRATCH_DIRECTORY_SCAN_BUDGET = 64

# Fixed reason codes (Issue #2200 In Scope). Never a free-form string --
# every caller of `_emit_artifact_confinement_failure()` /
# `_bounded_cleanup_stale_artifact_leftovers()` passes one of exactly these.
ARTIFACT_CONFINEMENT_REASON_CLEANUP_FAILED = "cleanup_failed"
ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE = "artifact_escape"
ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED = "artifact_oversized"
ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT = "stale_artifact"

# `_atomic_write_json()`/`_atomic_write_text()` in `run_refinement_preflight.py`
# both use this exact `tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
# dir=path.parent)` naming shape for their crash-safe rename-into-place
# writes, and already `finally: temporary.unlink()` it on every normal
# completion (success or exception) -- a survivor of that pattern can only
# mean the writing process was killed between `mkstemp()` and that `finally`
# (e.g. the Issue #2075 supervisor's SIGKILL escalation on a hung child).
# This is therefore the ONLY filename shape bounded cleanup ever considers;
# it never does a generic "delete anything old" sweep.
_LEFTOVER_SCRATCH_TEMP_PATTERN = re.compile(r"^\.[^/]+\.tmp$")


def _no_follow_path_components(root_real: str, candidate_unresolved_abs: str) -> bool:
    """True iff every path component from `root_real` down to
    `candidate_unresolved_abs` (inclusive of the final component) is
    confirmed to NOT be a symlink via `Path.is_symlink()` on each
    intermediate prefix.

    This is a component-wise walk over the UNRESOLVED candidate path
    (Issue #2200 AC4) -- deliberately never `os.path.realpath()`'d before
    this check runs. `realpath()` silently follows through every
    intermediate symlink and returns an already-resolved string with no
    symlink component left to detect; checking components AFTER that
    resolution would make this check a tautology (a fully-resolved path's
    own components are never symlinks by construction). The caller must
    pass the path exactly as it names the artifact on disk, only
    normalized (not resolved) and joined with `project_root` if relative.
    """
    try:
        rel = os.path.relpath(os.path.normpath(candidate_unresolved_abs), root_real)
    except ValueError:
        return False
    if rel == os.curdir or rel.startswith(os.pardir):
        return False
    current = Path(root_real)
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _open_confined_regular_fd(candidate_path: str, project_root: str) -> tuple[int, int] | None:
    """Open `candidate_path` read-only for confinement validation (Issue
    #2200 AC4). Returns `(fd, size)` or `None` on ANY confinement
    violation (the fd is always closed before returning `None`).

    Order of checks, all against the SAME opened fd (never a separate
    `stat()`/`lstat()` call on the path string, which would be a TOCTOU
    race against the fd this function hands back for reading):

    1. component-wise no-follow walk from `project_root`, over the
       UNRESOLVED candidate path (never a pre-computed `realpath()`)
    2. `os.open(..., O_NOFOLLOW)` on that SAME unresolved leaf (rejects a
       leaf that is itself a symlink, independent of #1 -- POSIX
       `O_NOFOLLOW` only ever guards the trailing path component)
    3. `os.fstat(fd)` confirms `S_ISREG` on the OPENED fd -- never a
       directory, FIFO, device, or socket masquerading as a result file
    """
    root_real = os.path.realpath(project_root)
    candidate_abs = (
        candidate_path
        if os.path.isabs(candidate_path)
        else str(Path(project_root) / candidate_path)
    )
    if not _no_follow_path_components(root_real, candidate_abs):
        return None
    try:
        # `O_NONBLOCK` is load-bearing here, not merely defensive: opening a
        # FIFO `O_RDONLY` WITHOUT it blocks the caller until some writer
        # opens the same path, which would turn a non-regular-file rejection
        # into an indefinite hang instead of a fast, fail-closed
        # `artifact_escape`. It has no effect on an ordinary regular file
        # (the actual, expected case) -- reads proceed normally below.
        fd = os.open(candidate_abs, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        file_stat = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        return None
    return fd, file_stat.st_size


def _read_bounded_fd(fd: int, max_bytes: int) -> bytes | None:
    """Read at most `max_bytes` from `fd`. Returns `None` (never a
    silently-truncated partial read) if the content exceeds `max_bytes`."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# Issue #2200 PR #2557 review fix-delta (P1-1): the EXACT literal
# `run_refinement_preflight.py`'s canonical result producer emits as its
# own `schema_version` field (that script's `SCHEMA_VERSION_RESULT`
# constant). Only an artifact that self-declares THIS EXACT value gets the
# strengthened provenance checks below; any other/absent `schema_version`
# keeps the pre-existing issue_number-only "legitimate omission" behavior
# byte-identical to before this fix_delta.
_CANONICAL_PREFLIGHT_RESULT_SCHEMA_VERSION = "refinement_preflight_result/v1"


def _strip_sha256_prefix(value: object) -> str | None:
    """Normalize a `sha256:<hex>` or bare-`<hex>` digest string to its bare
    lowercase hex form. Mirrors `run_refinement_preflight.py`'s own
    `_repair_apply_strip_sha_prefix()` (duplicated rather than
    cross-imported -- this module never imports a skill script)."""
    if not isinstance(value, str) or not value:
        return None
    value = value.removeprefix("sha256:")
    return value.lower()


def _artifact_owner_field_mismatch(
    payload: bytes, issue_number: str, repo: str | None, project_root: str
) -> bool:
    """Issue #2200 AC4 freshness/ownership check: when the artifact's own
    JSON body declares a top-level `issue_number` field, it must match the
    dispatch's `issue_number`. A parse failure, a non-dict payload, or the
    field's outright absence is a LEGITIMATE OMISSION (never a
    mismatch) -- this function only ever returns True for a field that IS
    present and DOES disagree, never for content this validator cannot
    read as declaring ownership at all (Issue #2200 AC4: "JSON内の任意文字列や
    `must_read` を生成artifactと誤分類しない" -- this reads exactly one fixed,
    already-schema'd field, never a generic string scan).

    PR #2557 review fix-delta (P1-1): when `repo` (the dispatch's own
    known repo slug) is supplied and the artifact ALSO declares a top-level
    `repo` field, that field is checked the same way as `issue_number`
    above. When the artifact additionally self-declares the canonical
    `schema_version` (`_CANONICAL_PREFLIGHT_RESULT_SCHEMA_VERSION`), this
    also reads the SAME already-schema'd `repair_action.preflight_run_identity`
    field as an additional freshness signal (a canonical-schema artifact
    that omits it can never be trusted as fresh), and -- when a
    `repair_action.candidate_body_artifact` sidecar is present alongside
    its own `repaired_body_sha256` -- verifies that sidecar's digest
    through the SAME no-follow-opened, same-fd-fstat-confirmed
    regular-file reader `_validate_artifact_confinement_bounds()` already
    uses for the artifact itself (never a new open helper, never a generic
    string scan, never a new registry/lease/timestamp protocol). Any
    `schema_version` other than the exact canonical value -- including its
    absence -- skips all of this and stays byte-identical to the
    pre-existing issue_number-only check."""
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    candidate_issue = parsed.get("issue_number")
    if candidate_issue is not None and str(candidate_issue) != str(issue_number):
        return True
    if repo is not None:
        candidate_repo = parsed.get("repo")
        if candidate_repo is not None and str(candidate_repo) != str(repo):
            return True
    if parsed.get("schema_version") != _CANONICAL_PREFLIGHT_RESULT_SCHEMA_VERSION:
        return False
    repair_action = parsed.get("repair_action")
    if not isinstance(repair_action, dict):
        return False
    if repair_action.get("preflight_run_identity") is None:
        return True
    candidate_body_artifact = repair_action.get("candidate_body_artifact")
    expected_digest = _strip_sha256_prefix(repair_action.get("repaired_body_sha256"))
    if not candidate_body_artifact or not expected_digest:
        return False
    candidate_path = (
        candidate_body_artifact
        if os.path.isabs(candidate_body_artifact)
        else str(Path(project_root) / candidate_body_artifact)
    )
    opened = _open_confined_regular_fd(candidate_path, project_root)
    if opened is None:
        return True
    fd, size = opened
    try:
        if size > ARTIFACT_CONFINEMENT_MAX_FILE_BYTES:
            return True
        body_bytes = _read_bounded_fd(fd, ARTIFACT_CONFINEMENT_MAX_FILE_BYTES)
    finally:
        os.close(fd)
    if body_bytes is None:
        return True
    return hashlib.sha256(body_bytes).hexdigest() != expected_digest


def _validate_artifact_confinement_bounds(
    project_root: str, issue_number: str, artifact_paths: list[str], repo: str | None = None
) -> tuple[str | None, list[str]]:
    """Issue #2200 AC4: confinement bounds for the compact stdout artifact
    projection, layered strictly ON TOP of `_validate_stdout_artifact_projection()`'s
    existing allowed-root check (this function assumes every path it is
    given has ALREADY passed that check -- it never re-derives or widens
    the allowed-root membership decision itself).

    Returns `(reason_code, offending_paths)`; `reason_code` is `None` when
    every artifact is within bounds (including the trivial case of zero
    artifacts -- a command whose status/execution path legitimately
    produces none is never treated as a missing-artifact error here).
    Never re-serializes/re-encodes any artifact -- only a bounded read
    through the no-follow-opened, same-fd-fstat-confirmed regular-file
    descriptor above.
    """
    if len(artifact_paths) > ARTIFACT_CONFINEMENT_MAX_COUNT:
        return ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED, list(artifact_paths)
    total_bytes = 0
    for raw_path in artifact_paths:
        # Deliberately NOT `os.path.realpath()`'d here -- `_open_confined_regular_fd()`
        # itself needs the UNRESOLVED path (see its own docstring) to detect
        # a symlink at any component. Only join with `project_root` when
        # `raw_path` is relative; never resolve.
        opened = _open_confined_regular_fd(raw_path, project_root)
        if opened is None:
            return ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE, [raw_path]
        fd, size = opened
        try:
            if size > ARTIFACT_CONFINEMENT_MAX_FILE_BYTES:
                return ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED, [raw_path]
            total_bytes += size
            if total_bytes > ARTIFACT_CONFINEMENT_MAX_TOTAL_BYTES:
                return ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED, [raw_path]
            payload = _read_bounded_fd(fd, ARTIFACT_CONFINEMENT_MAX_FILE_BYTES)
        finally:
            os.close(fd)
        if payload is None:
            return ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED, [raw_path]
        if _artifact_owner_field_mismatch(payload, issue_number, repo, project_root):
            return ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT, [raw_path]
    return None, []


def _bounded_cleanup_stale_artifact_leftovers(
    dispatch_root: str, issue_number: str, command_id: str, not_before_mtime: float
) -> tuple[bool, str | None, list[str]]:
    """Issue #2200 In Scope category 1 (unpublished temporary files /
    failed-run leftovers): best-effort, strictly-bounded removal of exactly
    the `_atomic_write_json()`/`_atomic_write_text()` `mkstemp()` scratch
    naming shape (`_LEFTOVER_SCRATCH_TEMP_PATTERN`) sitting DIRECTLY inside
    this command_id/issue_number's own already-verified allowed artifact
    root(s) (never a nested-subdirectory walk, never any other filename
    shape).

    A candidate is eligible for removal only when its own `lstat().st_mtime`
    is STRICTLY BEFORE `not_before_mtime` (the caller's own before-dispatch
    snapshot time) -- this dispatch's own still-running child can never have
    its own in-flight scratch file removed out from under it. Never touches
    the published `refinement_preflight_result_v1.json` (or any other
    non-`.tmp`-suffixed) artifact.

    Returns `(ok, reason_code, removed_paths)`. `reason_code` is one of
    `cleanup_failed`/`artifact_escape`/`artifact_oversized` on failure
    (`ok is False`), `None` on success (removing nothing, including a
    not-yet-existing artifact root, is a trivial success).

    PR #2557 review fix-delta (P2-4): the directory is enumerated via a
    streaming `os.scandir()` bounded by `_LEFTOVER_SCRATCH_DIRECTORY_SCAN_BUDGET`
    (a fixed, non-caller-configurable scan-cost ceiling, separate from
    `ARTIFACT_CONFINEMENT_MAX_COUNT`'s post-filter candidate-count cap
    below) -- never a full `list(root.iterdir())` materialization first.
    Exceeding the scan budget before the directory is fully enumerated
    fails closed exactly like an oversized candidate count."""
    removed: list[str] = []
    for root in _allowed_artifact_roots(dispatch_root, issue_number, command_id):
        if root.is_symlink() or not root.is_dir():
            continue
        candidates: list[Path] = []
        scanned = 0
        scan_budget_exceeded = False
        try:
            with os.scandir(root) as directory_iterator:
                for entry in directory_iterator:
                    scanned += 1
                    if scanned > _LEFTOVER_SCRATCH_DIRECTORY_SCAN_BUDGET:
                        scan_budget_exceeded = True
                        break
                    if _LEFTOVER_SCRATCH_TEMP_PATTERN.match(entry.name):
                        candidates.append(Path(entry.path))
        except OSError:
            return False, ARTIFACT_CONFINEMENT_REASON_CLEANUP_FAILED, removed
        if scan_budget_exceeded or len(candidates) > ARTIFACT_CONFINEMENT_MAX_COUNT:
            return False, ARTIFACT_CONFINEMENT_REASON_ARTIFACT_OVERSIZED, removed
        for entry in candidates:
            if entry.is_symlink():
                return False, ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE, removed
            try:
                entry_stat = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                return False, ARTIFACT_CONFINEMENT_REASON_ARTIFACT_ESCAPE, removed
            if entry_stat.st_mtime >= not_before_mtime:
                continue
            try:
                entry.unlink()
            except OSError:
                return False, ARTIFACT_CONFINEMENT_REASON_CLEANUP_FAILED, removed
            removed.append(str(entry))
    return True, None, removed


def _emit_artifact_confinement_failure(
    issue_number: int, reason_code: str, offending_paths: list[str]
) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code={reason_code} target_issue={issue_number} "
        f"artifact_path={','.join(offending_paths)} "
        "recovery=treat_as_stale_artifact_and_defer_to_fresh_preflight",
        file=sys.stderr,
    )
    return 2


def _emit_stale_runtime_failure(issue_number: int, stale_entries: list[tuple[str, str]]) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=stale_worktree_runtime_state target_issue={issue_number} "
        f"stale_path={','.join(path for _, path in stale_entries)} "
        f"source_env={','.join(env for env, _ in stale_entries)} "
        "recovery=unset_or_correct_runtime_env_to_issue_artifacts_root",
        file=sys.stderr,
    )
    return 2


def _format_inner_transaction_evidence_fields(extra_fields: dict[str, str] | None) -> str:
    """Render `_resolve_inner_transaction_evidence_fields()`'s output as
    additional ``key=value `` tokens for an outer-failure `SKILL_RUNTIME_FAIL`
    line (Issue #2393 P1-B). Empty for every command_id other than the two
    contract-update mutation profiles (`extra_fields` is `{}`/`None` there),
    so this is a byte-identical no-op for every other consumer of these
    emitters."""
    if not extra_fields:
        return ""
    return "".join(f"{key}={value} " for key, value in extra_fields.items())


def _emit_artifact_projection_failure(
    issue_number: int, stale_paths: list[str], *, extra_fields: dict[str, str] | None = None
) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=stale_worktree_runtime_state target_issue={issue_number} "
        f"stale_path={','.join(stale_paths)} "
        f"{_format_inner_transaction_evidence_fields(extra_fields)}"
        "recovery=do_not_publish_artifact_projection_outside_issue_artifact_root",
        file=sys.stderr,
    )
    return 2


def _emit_unauthorized_write_failure(
    issue_number: int, unauthorized_path: str, *, extra_fields: dict[str, str] | None = None
) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=unauthorized_write_path target_issue={issue_number} "
        f"unauthorized write path={unauthorized_path} "
        f"{_format_inner_transaction_evidence_fields(extra_fields)}"
        "recovery=do_not_write_outside_allowed_root",
        file=sys.stderr,
    )
    return 2


# Issue #2161 (native Codex CLI retirement): _emit_ledger_transient_residue_failure()
# was removed -- it was already dead code (never called) before this Issue,
# since Issue #1830 made launch-ledger residue advisory-only.


# ---------------------------------------------------------------------------
# Issue #2075: Popen-based outer-child supervisor.
#
# The previous implementation used `subprocess.run(child_argv, ...,
# timeout=timeout_seconds)` and reacted to `subprocess.TimeoutExpired` after
# the fact. That pattern cannot deliver a process-group cleanup guarantee:
# CPython's `subprocess.run()` already `kill()`s and `wait()`s its own direct
# child *before* re-raising `TimeoutExpired`, so by the time the `except`
# block runs there is no safe PID/PGID left for `os.killpg(...)` to act on
# (see the OWNER review on Issue #2075, and
# https://docs.python.org/3.12/library/subprocess.html).
#
# This module instead launches the child with `subprocess.Popen(...,
# start_new_session=True)` so the caller holds the child's PID/PGID from the
# moment it starts, and owns the full timeout + cleanup lifecycle:
#
#   execution timeout -> SIGTERM -> bounded grace -> SIGKILL
#     -> process-group absence verification -> direct-child (leader) reap
#
# Guarantee scope (Issue #2075 Outcome / OWNER review, narrowed from the
# original "reap the whole process tree" framing): only descendants that
# remain inside the executor-created process group (the common case for
# ordinary fork/exec descendants that never call `setsid()`/`setpgid()`
# themselves) are covered. A descendant that moves itself to a different
# session/process group is out of scope -- confirming its absence would
# require a subreaper/cgroup-level mechanism this module does not implement.
#
# `start_new_session` / `os.killpg` / `os.setsid` are POSIX-only. On a
# platform lacking them, this module falls back to direct-child-only
# termination and never reports `cleanup_status=confirmed_absent` (AC8):
# absence of a POSIX guarantee is reported as `unconfirmed`, not silently
# upgraded to a false "success".
# ---------------------------------------------------------------------------

_POSIX_PROCESS_GROUP_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "killpg")
    and hasattr(os, "setsid")
    and hasattr(os, "getpgid")
)

# Two independently bounded budgets govern outer-child supervision (Issue
# #2075 P1-4 OWNER review contract):
#
#   1. `timeout_seconds` (caller-supplied, per registry entry) bounds only
#      the *execution* wait -- i.e. `proc.communicate(timeout=...)` below.
#   2. `_CLEANUP_GRACE_SECONDS` is a separate, freshly-started budget for
#      the *entire* SIGTERM -> process-group-liveness poll -> SIGKILL ->
#      absence verification -> leader reap -> pipe close sequence, begun
#      only once the execution deadline has already been exceeded (or an
#      exception unwinds past a successful `Popen()`). It is never stacked
#      on top of the execution deadline, and no cleanup step below is ever
#      given a further, separate ad-hoc timeout on top of it -- every
#      cleanup step is bounded by `remaining(cleanup_deadline)`.
_CLEANUP_GRACE_SECONDS = 5.0
# Portion of `_CLEANUP_GRACE_SECONDS` allotted to waiting for SIGTERM to take
# effect before escalating to SIGKILL. Must stay smaller than
# `_CLEANUP_GRACE_SECONDS` so the escalation + absence-verification + reap
# steps that follow always retain some of the shared cleanup budget, instead
# of a SIGTERM-ignoring child silently consuming the entire cleanup deadline
# and leaving no time to ever send SIGKILL.
_TERM_GRACE_SECONDS = 2.0
_GROUP_POLL_INTERVAL_SECONDS = 0.02

CLEANUP_SCOPE_PROCESS_GROUP = "process_group"
CLEANUP_STATUS_CONFIRMED_ABSENT = "confirmed_absent"
CLEANUP_STATUS_UNCONFIRMED = "unconfirmed"
CLEANUP_STATUS_NOT_STARTED = "not_started"
TERMINATION_TERM = "term"
TERMINATION_TERM_THEN_KILL = "term_then_kill"
TERMINATION_NOT_NEEDED = "not_needed"


class _ChildSupervisionResult:
    """Outcome of `_run_child_with_supervision()` (Issue #2075)."""

    __slots__ = (
        "cleanup_scope",
        "cleanup_status",
        "leader_reaped",
        "pid",
        "returncode",
        "stderr",
        "stdout",
        "termination",
        "timed_out",
    )

    def __init__(
        self,
        *,
        timed_out: bool,
        returncode: int | None,
        stdout: str | bytes,
        stderr: str | bytes,
        cleanup_scope: str,
        cleanup_status: str,
        termination: str,
        leader_reaped: bool,
        pid: int | None = None,
    ) -> None:
        self.timed_out = timed_out
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cleanup_scope = cleanup_scope
        self.cleanup_status = cleanup_status
        self.termination = termination
        self.leader_reaped = leader_reaped
        # `pid` is an introspection aid (not part of the closed telemetry
        # enum emitted by `_emit_timeout_failure()`) that lets a caller /
        # test independently confirm the direct child was actually reaped.
        self.pid = pid


def _remaining(deadline: float) -> float:
    """Return the non-negative seconds remaining until `deadline`."""
    return max(0.0, deadline - time.monotonic())


def _verify_process_group_absent(pgid: int) -> bool:
    """Return True only when `killpg(pgid, 0)` proves the group is gone
    (`ProcessLookupError`). Every other outcome -- the group is still alive,
    a `PermissionError`, or any other `OSError` -- must NOT be treated as
    confirmed absence (Issue #2075 AC6/AC9 fail-closed contract: cleanup
    success is never inferred from signal dispatch alone)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _poll_group_absent_until(proc: subprocess.Popen, pgid: int, deadline: float) -> bool:
    """Poll `_verify_process_group_absent(pgid)` until it is True or
    `deadline` elapses. Returns True iff absence was confirmed within the
    deadline.

    The escalation/absence decision itself is driven entirely by
    *process-group* liveness (`killpg(pgid, 0)`), never by leader
    (direct-child) liveness (Issue #2075 P1-1 OWNER review): a leader that
    exits early while a SIGTERM-ignoring descendant survives in the same
    process group must never suppress the SIGKILL escalation that follows.

    Each iteration also opportunistically calls `proc.poll()` (a
    non-blocking `waitpid()`) purely as a side effect so a leader that has
    already died does not linger as an unreaped zombie -- an unreaped
    zombie's PID slot still answers `kill(pid, 0)` successfully, which
    would otherwise make `killpg()` report the group as still alive even
    though every process in it has actually exited. This reap is
    incidental cleanup, not a liveness signal: it never gates the
    SIGTERM -> SIGKILL escalation decision above."""
    while True:
        proc.poll()
        if _verify_process_group_absent(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)


def _bounded_reap_leader(proc: subprocess.Popen, deadline: float) -> bool:
    """Best-effort `proc.wait()` bounded by `deadline`. Returns True iff the
    direct child leader was reaped (no zombie left behind)."""
    try:
        proc.wait(timeout=_remaining(deadline))
        return True
    except subprocess.TimeoutExpired:
        return False


def _bounded_close_pipes(proc: subprocess.Popen) -> None:
    """Best-effort close of the leader's stdout/stderr pipes so a
    descendant that still holds the write end open can never block this
    process from returning (Issue #2075 AC7: no partial output is ever
    surfaced on timeout regardless of how/when this closes)."""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _stage_cleanup(
    proc: subprocess.Popen,
    pgid: int | None,
    *,
    posix_supervised: bool,
) -> tuple[str, str, str, bool]:
    """Run the full staged cleanup state machine -- SIGTERM, process-group
    liveness poll, SIGKILL escalation, absence verification, leader reap,
    pipe close -- under a single, freshly-started `cleanup_deadline`
    (`_CLEANUP_GRACE_SECONDS`) that is independent of whatever execution
    deadline just elapsed (Issue #2075 P1-4). Every step below is bounded
    by `remaining(cleanup_deadline)`; no step gets its own separate,
    unbounded-relative-to-the-caller ad-hoc timeout.

    Used from both the `TimeoutExpired` path and the `except BaseException`
    unwind path in `_run_child_with_supervision()` so that every exit path
    that follows a successful `Popen()` drives the process group through
    this exact same state machine (Issue #2075 P1-3).

    Returns `(cleanup_scope, cleanup_status, termination, leader_reaped)`.
    """
    cleanup_deadline = time.monotonic() + _CLEANUP_GRACE_SECONDS
    term_deadline = min(cleanup_deadline, time.monotonic() + _TERM_GRACE_SECONDS)

    group_supervised = posix_supervised and pgid is not None

    if group_supervised:
        termination = TERMINATION_NOT_NEEDED
        try:
            os.killpg(pgid, signal.SIGTERM)
            termination = TERMINATION_TERM
        except ProcessLookupError:
            termination = TERMINATION_NOT_NEEDED
        except OSError:
            termination = TERMINATION_TERM

        # P1-1: the SIGTERM -> SIGKILL escalation decision is driven by
        # *process-group* liveness, not leader liveness -- a leader that
        # exits (on its own, or from the SIGTERM) while a SIGTERM-ignoring
        # descendant survives in the same group must not suppress SIGKILL.
        group_absent = _poll_group_absent_until(proc, pgid, term_deadline)
        if not group_absent and time.monotonic() < cleanup_deadline:
            try:
                os.killpg(pgid, signal.SIGKILL)
                termination = TERMINATION_TERM_THEN_KILL
            except ProcessLookupError:
                pass
            except OSError:
                pass
            group_absent = _poll_group_absent_until(proc, pgid, cleanup_deadline)

        # AC6: absence is only ever reported `confirmed_absent` while
        # cleanup budget remains; deadline exhaustion is never silently
        # promoted to a confirmed success.
        if time.monotonic() >= cleanup_deadline:
            cleanup_status = CLEANUP_STATUS_UNCONFIRMED
        else:
            cleanup_status = (
                CLEANUP_STATUS_CONFIRMED_ABSENT if group_absent else CLEANUP_STATUS_UNCONFIRMED
            )
    else:
        # AC8: no `killpg`/`setsid` on this platform (or no pgid to
        # supervise) -- best-effort terminate the direct child only, and
        # never claim process-group absence was confirmed. There is no
        # POSIX guarantee here to confirm (P2-1: a failed pgid lookup must
        # never be promoted to `confirmed_absent` either).
        termination = TERMINATION_TERM
        try:
            proc.terminate()
        except OSError:
            pass
        if not _bounded_reap_leader(proc, term_deadline):
            termination = TERMINATION_TERM_THEN_KILL
            try:
                proc.kill()
            except OSError:
                pass
        cleanup_status = CLEANUP_STATUS_UNCONFIRMED

    leader_reaped = _bounded_reap_leader(proc, cleanup_deadline)
    _bounded_close_pipes(proc)

    return CLEANUP_SCOPE_PROCESS_GROUP, cleanup_status, termination, leader_reaped


def _run_child_with_supervision(
    child_argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: float | None,
    binary_output: bool = False,
) -> _ChildSupervisionResult:
    """Launch `child_argv` under direct caller supervision.

    Generic commands retain the previous text-mode normal-success semantics.
    The structural transport opts into binary capture so its stdout/stderr
    relay preserves CRLF and invalid UTF-8 byte-for-byte (Issue #2402 AC4).
    The execution wait is delegated to `proc.communicate(timeout=...)`;
    a leader that exits while a descendant still holds stdout/stderr open
    remains blocked (and can time out) rather than being mistaken for success.

    On timeout (or any other exception unwinding past a successful
    `Popen()`, including `KeyboardInterrupt`), the process group is driven
    through `_stage_cleanup()`'s bounded state machine before returning /
    re-raising (Issue #2075 P1-3). On timeout, no partial stdout/stderr is
    ever surfaced (AC7) -- only cleanup telemetry is returned.
    """
    posix_supervised = _POSIX_PROCESS_GROUP_SUPPORTED
    empty_output: str | bytes = b"" if binary_output else ""

    popen_kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": env,
        "shell": False,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": not binary_output,
    }
    if posix_supervised:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(child_argv, **popen_kwargs)  # type: ignore[arg-type]
    # P2-1: capture the process-group id at launch time, as `proc.pid`
    # itself -- not via a later `os.getpgid()` lookup. `start_new_session`
    # makes the leader its own session/group leader, so `proc.pid` *is* the
    # pgid from the instant `Popen()` returns; there is no window in which a
    # delayed `getpgid()` call could race, and a lookup failure can never be
    # (mis)promoted to `confirmed_absent` because this code path never
    # performs that lookup at all.
    pgid: int | None = proc.pid if posix_supervised else None

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        cleanup_scope, cleanup_status, termination, leader_reaped = _stage_cleanup(
            proc, pgid, posix_supervised=posix_supervised
        )
        return _ChildSupervisionResult(
            timed_out=True,
            returncode=None,
            stdout=empty_output,
            stderr=empty_output,
            cleanup_scope=cleanup_scope,
            cleanup_status=cleanup_status,
            termination=termination,
            leader_reaped=leader_reaped,
            pid=proc.pid,
        )
    except BaseException:
        # P1-3: any other exception after a successful Popen() -- including
        # KeyboardInterrupt -- must still drive the process group through
        # the same bounded cleanup state machine before propagating, or a
        # detached process group (start_new_session=True) can outlive this
        # process entirely.
        _stage_cleanup(proc, pgid, posix_supervised=posix_supervised)
        raise
    else:
        return _ChildSupervisionResult(
            timed_out=False,
            returncode=proc.returncode,
            stdout=stdout if stdout is not None else empty_output,
            stderr=stderr if stderr is not None else empty_output,
            cleanup_scope=CLEANUP_SCOPE_PROCESS_GROUP,
            cleanup_status=CLEANUP_STATUS_NOT_STARTED,
            termination=TERMINATION_NOT_NEEDED,
            leader_reaped=True,
            pid=proc.pid,
        )


def _emit_timeout_failure(
    issue_number: int,
    timeout_seconds: object,
    *,
    cleanup_scope: str = CLEANUP_SCOPE_PROCESS_GROUP,
    cleanup_status: str = CLEANUP_STATUS_NOT_STARTED,
    termination: str = TERMINATION_NOT_NEEDED,
    leader_reaped: bool = False,
    extra_fields: dict[str, str] | None = None,
) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=child_process_timeout target_issue={issue_number} "
        f"timeout_seconds={timeout_seconds} "
        f"cleanup_scope={cleanup_scope} "
        f"cleanup_status={cleanup_status} "
        f"termination={termination} "
        f"leader_reaped={'true' if leader_reaped else 'false'} "
        f"{_format_inner_transaction_evidence_fields(extra_fields)}"
        "recovery=investigate_child_process_hang_or_increase_registry_timeout",
        file=sys.stderr,
    )
    return 2


def _emit_primary_checkout_drift_failure(issue_number: int) -> int:
    # Issue #2199 OWNER feedback P2(a): this is an unconditional
    # STOP-ON-ANY-OBSERVED-CHANGE safety net, not a proof that THIS
    # dispatch's own dedicated child caused the observed difference -- an
    # unrelated concurrent process touching the SAME primary checkout
    # during the same span trips this identically. The recovery guidance
    # below deliberately does not assert dedicated-dispatch causation.
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=primary_checkout_invariant_violated target_issue={issue_number} "
        "recovery=inspect_primary_checkout_for_any_state_change_observed_during_this_dispatch_span_cause_unattributed",
        file=sys.stderr,
    )
    return 2


def _contract_update_result_artifact_path(dispatch_root: str, issue_number: int) -> Path:
    """The canonical result artifact `run_refinement_preflight.py` writes
    (via `_write_artifacts()`/`write_provenance_artifact()`'s sibling
    `refinement_preflight_result_v1.json`) under the FIRST allowed artifact
    root for the two contract-update mutation profiles (Issue #2393 P1-B).
    This path lives under `.claude/artifacts/issue-refinement-loop/<issue>/`
    -- never under the transaction-local `tmp/` workspace the inner
    transaction deletes before returning -- so it survives past the span
    this function's caller monitors, and is safe to read back after that
    span ends."""
    return (
        Path(dispatch_root)
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(issue_number)
        / "refinement_preflight_result_v1.json"
    )


def _stat_identity_or_none(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _resolve_inner_transaction_evidence_fields(
    dispatch_root: str,
    issue_number: int,
    command_id: str,
    before_result_stat: tuple[int, int] | None,
) -> dict[str, str]:
    """Issue #2393 P1-B: on an OUTER dispatch failure (timeout /
    unauthorized-write / artifact-projection), attempt to recover a
    reference to an inner transaction result that may already have been
    achieved (a real GitHub mutation + readback) before the outer failure
    was detected -- read-only, never writing a new file here.

    Only applies to the two contract-update mutation profiles; every other
    command_id gets `{}` (a byte-identical no-op for the emitted stderr
    line).

    Freshness guard: the result artifact's `(mtime_ns, size)` identity is
    compared against `before_result_stat` (captured BEFORE this dispatch's
    child ran). A leftover artifact from a PRIOR, unrelated dispatch that
    this span's child never touched must never be misreported as "achieved
    by this span" -- if the identity is unchanged (or the file still does
    not exist), this returns the explicit `inner_transaction_state=unknown`
    marker, never a fabricated disposition and never a false
    "unmutated"/"no-op" default.
    """
    if command_id not in CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS:
        return {}
    result_path = _contract_update_result_artifact_path(dispatch_root, issue_number)
    after_result_stat = _stat_identity_or_none(result_path)
    if after_result_stat is None or after_result_stat == before_result_stat:
        return {"inner_transaction_state": "unknown"}
    try:
        parsed_result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"inner_transaction_state": "unknown"}
    contract_update = (
        parsed_result.get("contract_update") if isinstance(parsed_result, dict) else None
    )
    disposition = contract_update.get("disposition") if isinstance(contract_update, dict) else None
    if not isinstance(disposition, str) or not disposition:
        return {"inner_transaction_state": "unknown"}
    return {
        "inner_transaction_result_ref": str(result_path),
        "inner_transaction_disposition": disposition,
    }


def _dispatch_child_and_check_postconditions(
    *,
    dispatch_root: str,
    issue_number: int,
    command_id: str,
    child_argv: list[str],
    env: dict[str, str],
    timeout_seconds: object,
    binary_output: bool,
    repo: str | None = None,
) -> int:
    """Dispatch the child at ``dispatch_root`` and run the existing
    before/after post-child checks against that SAME root (Issue #2199
    AC9): before/after repo snapshot, unauthorized-write detection, and
    stdout/artifact projection validation. ``dispatch_root`` is
    ``project_root`` for every command_id except the 4 production preflight
    profiles (Issue #2199 In Scope), which pass their dedicated
    ``execution_root`` here instead -- this function itself has no
    production/fixture branching of its own, so the caller's root selection
    is the only thing that ever changes which checkout is touched.
    """
    if command_id in CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS:
        # Issue #2393 P1-A: a genuinely cold dedicated worktree (one that has
        # never run a contract_update dispatch before) has no `tmp/`
        # directory yet. The inner edit-issue transaction creates `tmp/` for
        # its candidate/input files and deletes those FILES (never the
        # directory itself) before returning -- leaving a newly-appeared,
        # now-empty `tmp/` directory that `_snapshot_repo_paths()` already
        # excludes from ITS OWN filesystem-walk diff (see the `root / "tmp"`
        # addition to `allowed_parent_dirs` above), but that
        # `_find_unauthorized_repo_changes()`'s SEPARATE `git status`-based
        # diff (`before_status`/`after_status`) has no equivalent exclusion
        # for: a cold-start `!! tmp/` entry with no counterpart in
        # `before_status` is misreported as an unauthorized write. Creating
        # `tmp/` here, BEFORE `before_snapshot`/`before_status` are captured
        # below, makes it present on BOTH sides of both diffs, closing the
        # gap without weakening either check or excluding `tmp/**` wholesale
        # (a residual child path left inside `tmp/` after the child returns
        # is still rejected by both diffs, exactly as before).
        (Path(dispatch_root) / "tmp").mkdir(parents=True, exist_ok=True)
    # Issue #2584 In Scope item 5: a SEPARATE, independent gate performing
    # the SAME cold-worktree `tmp/` pre-creation for the 3 additional
    # command_ids that also dispatch through `edit_issue_txn.py`'s
    # transaction core outside the `CONTRACT_UPDATE_MUTATION_DEDICATED_
    # COMMAND_IDS` branch above (owner decision: this branch must never be
    # merged with, or have its own contract-update-specific rationale
    # reused by, that branch -- see `TMP_DIRECTORY_NODE_HOUSEKEEPING_
    # COMMAND_IDS`'s own module-level docstring). `mkdir(..., exist_ok=True)`
    # is idempotent, so both branches firing for a `CONTRACT_UPDATE_
    # MUTATION_DEDICATED_COMMAND_IDS` member (which is ALSO a member of this
    # frozenset) is a harmless no-op, never a double-creation.
    if command_id in TMP_DIRECTORY_NODE_HOUSEKEEPING_COMMAND_IDS:
        (Path(dispatch_root) / "tmp").mkdir(parents=True, exist_ok=True)
    # Issue #2393 P1-B: captured BEFORE the child runs, so
    # `_resolve_inner_transaction_evidence_fields()` can later distinguish
    # "this span's own child achieved a fresh result" from "a stale artifact
    # a prior, unrelated dispatch already left on disk".
    before_result_stat = (
        _stat_identity_or_none(_contract_update_result_artifact_path(dispatch_root, issue_number))
        if command_id in CONTRACT_UPDATE_MUTATION_DEDICATED_COMMAND_IDS
        else None
    )
    before_snapshot = _snapshot_repo_paths(dispatch_root, str(issue_number), command_id)
    before_status = _git_status_paths(dispatch_root)
    # Issue #2200: wall-clock mark for the bounded-cleanup mtime guard below
    # (`_bounded_cleanup_stale_artifact_leftovers()`'s `not_before_mtime`) --
    # captured at the same point as the other before-snapshots, strictly
    # before the child ever runs, so a scratch file THIS span's own child
    # creates can never be misidentified as a pre-existing leftover.
    before_dispatch_wall_time = time.time()

    supervision = _run_child_with_supervision(
        child_argv,
        cwd=dispatch_root,
        env=env,
        timeout_seconds=timeout_seconds,
        binary_output=binary_output,
    )
    if supervision.timed_out:
        return _emit_timeout_failure(
            issue_number,
            timeout_seconds,
            cleanup_scope=supervision.cleanup_scope,
            cleanup_status=supervision.cleanup_status,
            termination=supervision.termination,
            leader_reaped=supervision.leader_reaped,
            extra_fields=_resolve_inner_transaction_evidence_fields(
                dispatch_root, issue_number, command_id, before_result_stat
            ),
        )
    result = supervision

    unauthorized_path = _find_unauthorized_repo_changes(
        dispatch_root,
        str(issue_number),
        before_snapshot,
        before_status,
        command_id,
    )
    if unauthorized_path is not None:
        return _emit_unauthorized_write_failure(
            issue_number,
            unauthorized_path,
            extra_fields=_resolve_inner_transaction_evidence_fields(
                dispatch_root, issue_number, command_id, before_result_stat
            ),
        )

    stdout_for_artifact_projection = (
        result.stdout.decode("utf-8", errors="surrogateescape")
        if isinstance(result.stdout, bytes)
        else result.stdout
    )
    artifact_projection_failures = _validate_stdout_artifact_projection(
        dispatch_root,
        str(issue_number),
        stdout_for_artifact_projection,
        command_id,
    )
    if artifact_projection_failures:
        return _emit_artifact_projection_failure(
            issue_number,
            artifact_projection_failures,
            extra_fields=_resolve_inner_transaction_evidence_fields(
                dispatch_root, issue_number, command_id, before_result_stat
            ),
        )

    # Issue #2200 AC1/AC4/AC5: confinement bounds + bounded stale-leftover
    # cleanup, layered strictly ON TOP of the existing allowed-root check
    # above, and strictly BEFORE stdout is ever published below (preserving
    # the existing "検証 → 公開" ordering this Issue must not break). Scoped
    # to the 4 production preflight profiles only -- a byte-identical no-op
    # for the 2 contract_update mutation profiles and every other
    # command_id, which keep their existing, unmodified behavior.
    if command_id in ARTIFACT_CONFINEMENT_COMMAND_IDS:
        confinement_reason, confinement_paths = _validate_artifact_confinement_bounds(
            dispatch_root,
            str(issue_number),
            _parse_artifact_projection(stdout_for_artifact_projection),
            repo=repo,
        )
        if confinement_reason is not None:
            return _emit_artifact_confinement_failure(
                issue_number, confinement_reason, confinement_paths
            )

        cleanup_ok, cleanup_reason, _removed = _bounded_cleanup_stale_artifact_leftovers(
            dispatch_root, str(issue_number), command_id, before_dispatch_wall_time
        )
        if not cleanup_ok:
            return _emit_artifact_confinement_failure(
                issue_number, cleanup_reason or "cleanup_failed", []
            )

    if isinstance(result.stdout, bytes):
        sys.stdout.buffer.write(result.stdout)
    elif result.stdout:
        sys.stdout.write(result.stdout)
    if isinstance(result.stderr, bytes):
        sys.stderr.buffer.write(result.stderr)
    elif result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


_AGENT_OPS_DIR = Path(__file__).resolve().parent.parent / "agent-ops"


def _load_worktree_bootstrap_exec_module():
    """Late-bound import of ``worktree_bootstrap_exec`` (Issue #2199).

    Deferred to call time -- never imported at this module's own top level
    -- because ``worktree_bootstrap_exec.py`` itself does ``from
    skill_runtime_exec import (...)``; importing it eagerly from this
    module's top level would be a circular import.

    The ``sys.modules.setdefault`` below also makes this import safe when
    THIS file is the running ``__main__`` script (its normal production
    invocation shape, Issue #2199 AC7's unchanged outward command shape):
    without it, ``worktree_bootstrap_exec.py``'s own ``from
    skill_runtime_exec import ...`` would silently re-exec this entire file
    under a second, distinct module identity, producing a second, unequal
    copy of every exception type this module defines (e.g.
    ``ControlPlaneUnavailable``) that this module's own ``except`` clauses
    would then fail to match.
    """
    sys.modules.setdefault("skill_runtime_exec", sys.modules[__name__])
    if str(_AGENT_OPS_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_OPS_DIR))
    import worktree_bootstrap_exec

    return worktree_bootstrap_exec


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="Privileged exact skill runtime executor", allow_abbrev=False
    )
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--fixture", required=False, default=None)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    parser.add_argument("--loop-state-file", required=False, default=None)
    parser.add_argument("--review-result-verdict", required=False, default=None)
    parser.add_argument("--max-iterations", required=False, default=None)
    # #2086 AC9/AC10: authority_transport.produce / authority_transport.consume.
    parser.add_argument("--invocation-id", required=False, default=None)
    parser.add_argument("--git-head-sha", required=False, default=None)
    parser.add_argument("--evidence-fixture-path", required=False, default=None)
    parser.add_argument("--router-receipt-path", required=False, default=None)
    parser.add_argument("--contract-patch-plan-file", required=False, default=None)
    parser.add_argument("--anchor-context-file", required=False, default=None)
    # Issue #2688: owner_reaction.decide / owner_reaction.decide.fixture.
    # Issue #2689 P0-1 fix_delta: `--owner-user-id`/`--preview-binding-file`
    # are ALSO reused (same argparse flags, same value shapes) for
    # `preflight.run.with_human_context`/`contract_update.run.with_human_context`'s
    # own optional mutation-gate transport triple below.
    parser.add_argument("--owner-user-id", required=False, type=int, default=None)
    parser.add_argument("--preview-binding-file", required=False, default=None)
    parser.add_argument("--gh-fixture-file", required=False, default=None)
    parser.add_argument("--mutation-category", required=False, default=None)
    # Generic and structural mutation consumers have distinct command IDs and
    # exact outer flags. Argparse rejects a mixed invocation before dispatch;
    # each command branch below also enforces its exact pairing.
    apply_action_flags = parser.add_mutually_exclusive_group()
    apply_action_flags.add_argument("--apply-repair-action", required=False, default=None)
    apply_action_flags.add_argument(
        "--apply-structural-repair-action", required=False, default=None
    )
    # #2086 P0 fix_delta (Blocker 1/2): only ever meaningful for
    # preflight.run.with_human_context (the operator-selected human-context
    # lane) -- see skill_runtime_command_policy._parse_exact_skill_runtime_anchor_command.
    parser.add_argument("--investigation-evidence-transport-path", required=False, default=None)
    # #2086 P0 fix_delta (Blocker 3): decide.run "Mode B" -- the privileged
    # router additionally dispatching #2053's canonical authority-transport
    # verification (generate_router_receipt()) alongside its ordinary
    # loop-state decision, mirroring decide_next_loop_action.py's own
    # --authority-transport-path/--authority-expected CLI flags (added by PR
    # #2068, declared in command_registry.py's decide.run entry, but never
    # reachable through this executor before this fix_delta).
    parser.add_argument("--authority-transport-path", required=False, default=None)
    parser.add_argument("--authority-expected", action="store_true", default=False)
    # Argparse accepts duplicate options and keeps the final value.  Reject
    # the raw outer grammar first so a malformed invocation can never be
    # normalized into a valid child command.
    sibling_id = "preflight.run.fixture.with_human_context"
    if sibling_id in raw_argv or f"--command-id={sibling_id}" in raw_argv:
        raw_command = " ".join(["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL, *raw_argv])
        if not is_exact_skill_runtime_anchor_fixture_executor_command(
            raw_command, resolve_project_root(), resolve_project_root()
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    structural_id = "structural_repair_action.apply"
    if structural_id in raw_argv or f"--command-id={structural_id}" in raw_argv:
        # Validate raw argv before argparse can normalize a duplicate option.
        # This is an outer transport check only; the child owns payload parsing.
        raw_command = " ".join(["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL, *raw_argv])
        if not is_exact_skill_runtime_structural_repair_action_apply_executor_command(
            raw_command, resolve_project_root(), resolve_project_root()
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2

    # The producer has one fixed outer argv shape. Check raw tokens BEFORE
    # argparse can normalize duplicates or echo a user-controlled argument
    # into stderr. Keep the other command classes' parsers unchanged.
    if (
        "authority_transport.produce" in raw_argv
        or "--command-id=authority_transport.produce" in raw_argv
    ):
        producer_flags = (
            "--command-id",
            "--issue-number",
            "--repo",
            "--invocation-id",
            "--git-head-sha",
            "--evidence-fixture-path",
        )
        if (
            len(raw_argv) != 12
            or any(raw_argv[2 * i] != flag for i, flag in enumerate(producer_flags))
            or not raw_argv[3].isdigit()
            or int(raw_argv[3]) <= 0
        ):
            print("skill_runtime_exec: authority_transport.produce: invalid_argv", file=sys.stderr)
            return 2
        if not is_trusted_repo_slug(raw_argv[5]):
            print("skill_runtime_exec: authority_transport.produce: repo_mismatch", file=sys.stderr)
            return 2

    args = parser.parse_args(raw_argv)

    project_root = resolve_project_root()
    stale_entries = _normalize_and_validate_runtime_env(project_root)
    if stale_entries:
        return _emit_stale_runtime_failure(args.issue_number, stale_entries)

    # Issue #2651 OWNER review fix_delta (blocker 1): detect a legacy Spark
    # execution request on this executor's OWN input boundary, before
    # `_sanitize_env()` strips `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK` below
    # and before any child/inner preflight is dispatched. Must run ahead of
    # every other command-id branch below.
    legacy_spark_request = _detect_legacy_spark_execution_request(args.command_id)
    if legacy_spark_request is not None:
        spark_mode, spark_fallback = legacy_spark_request
        return _emit_legacy_spark_retired_rejection(args.issue_number, spark_mode, spark_fallback)

    is_fixture_command = args.command_id == "preflight.run.fixture"
    is_fixture_human_context_command = args.command_id == "preflight.run.fixture.with_human_context"
    is_anchor_command = args.command_id in {
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
    }
    is_contract_update_command = args.command_id in {
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
    }
    # Issue #2689 P0-1 fix_delta: the ONLY two command_ids that may carry
    # `--mutation-category`/`--owner-user-id`/`--preview-binding-file`.
    is_with_human_context_command = args.command_id in {
        "preflight.run.with_human_context",
        "contract_update.run.with_human_context",
    }
    is_decide_command = args.command_id == "decide.run"
    is_produce_command = args.command_id == "authority_transport.produce"
    is_consume_command = args.command_id == "authority_transport.consume"
    is_repair_action_apply_command = args.command_id == "repair_action.apply"
    is_structural_repair_action_apply_command = args.command_id == "structural_repair_action.apply"
    # Issue #2688: owner_reaction.decide / owner_reaction.decide.fixture
    # exact dispatch wiring (registry entries added in Issue #1975; the
    # generic-dispatch wiring itself was explicitly Out of Scope there).
    is_owner_reaction_decide_command = args.command_id == "owner_reaction.decide"
    is_owner_reaction_decide_fixture_command = args.command_id == "owner_reaction.decide.fixture"
    # #2086 P0 fix_delta (Blocker 3): decide.run may ALSO carry
    # --invocation-id/--git-head-sha (bound into its Mode B authority-check
    # sub-fields), in addition to authority_transport.produce/consume.
    if not (is_produce_command or is_consume_command or is_decide_command) and (
        args.invocation_id
        or args.git_head_sha
        or args.evidence_fixture_path
        or args.router_receipt_path
        or args.contract_patch_plan_file
        or args.anchor_context_file
    ):
        print(
            "skill_runtime_exec: --invocation-id/--git-head-sha/--evidence-fixture-path/"
            "--router-receipt-path/--contract-patch-plan-file/--anchor-context-file are "
            "only allowed for authority_transport.produce/authority_transport.consume/decide.run",
            file=sys.stderr,
        )
        return 2
    if not is_repair_action_apply_command and args.apply_repair_action:
        print(
            "skill_runtime_exec: --apply-repair-action is only allowed for repair_action.apply",
            file=sys.stderr,
        )
        return 2
    if is_repair_action_apply_command and not args.apply_repair_action:
        print(
            "skill_runtime_exec: --apply-repair-action is required for repair_action.apply",
            file=sys.stderr,
        )
        return 2
    if not is_structural_repair_action_apply_command and args.apply_structural_repair_action:
        print(
            "skill_runtime_exec: --apply-structural-repair-action is only allowed for structural_repair_action.apply",
            file=sys.stderr,
        )
        return 2
    if is_structural_repair_action_apply_command and not args.apply_structural_repair_action:
        print(
            "skill_runtime_exec: --apply-structural-repair-action is required for structural_repair_action.apply",
            file=sys.stderr,
        )
        return 2
    # Issue #2688 AC4 / Issue #2689 P0-1 fix_delta: exact required/forbidden
    # combination validation for --owner-user-id/--preview-binding-file
    # (production and fixture owner_reaction.decide profiles, PLUS the two
    # with_human_context profiles' own optional mutation-gate transport
    # triple) and fixture's --gh-fixture-file.
    if not (
        is_owner_reaction_decide_command
        or is_owner_reaction_decide_fixture_command
        or is_with_human_context_command
    ) and (args.owner_user_id or args.preview_binding_file):
        print(
            "skill_runtime_exec: --owner-user-id/--preview-binding-file are only allowed for "
            "owner_reaction.decide/owner_reaction.decide.fixture/"
            "preflight.run.with_human_context/contract_update.run.with_human_context",
            file=sys.stderr,
        )
        return 2
    if (is_owner_reaction_decide_command or is_owner_reaction_decide_fixture_command) and not (
        args.owner_user_id and args.preview_binding_file
    ):
        print(
            "skill_runtime_exec: --owner-user-id and --preview-binding-file are required for "
            "owner_reaction.decide/owner_reaction.decide.fixture",
            file=sys.stderr,
        )
        return 2
    if not is_with_human_context_command and args.mutation_category:
        print(
            "skill_runtime_exec: --mutation-category is only allowed for "
            "preflight.run.with_human_context/contract_update.run.with_human_context",
            file=sys.stderr,
        )
        return 2
    if is_with_human_context_command:
        # Issue #2689 P0-1 fix_delta: an atomic optional triple -- all three
        # supplied together, or none at all. A partial combination is a
        # contract violation (fail-closed exit 2), mirroring
        # `run_refinement_preflight.py`'s own `main()` validation of this
        # SAME triple.
        _mutation_gate_fields_present = (
            bool(args.mutation_category),
            args.owner_user_id is not None,
            bool(args.preview_binding_file),
        )
        if any(_mutation_gate_fields_present) and not all(_mutation_gate_fields_present):
            print(
                "skill_runtime_exec: --mutation-category/--owner-user-id/--preview-binding-file "
                "must be supplied together or not at all",
                file=sys.stderr,
            )
            return 2
    if not is_owner_reaction_decide_fixture_command and args.gh_fixture_file:
        print(
            "skill_runtime_exec: --gh-fixture-file is only allowed for owner_reaction.decide.fixture",
            file=sys.stderr,
        )
        return 2
    if is_owner_reaction_decide_fixture_command and not args.gh_fixture_file:
        print(
            "skill_runtime_exec: --gh-fixture-file is required for owner_reaction.decide.fixture",
            file=sys.stderr,
        )
        return 2
    if not is_decide_command and (args.authority_transport_path or args.authority_expected):
        print(
            "skill_runtime_exec: --authority-transport-path/--authority-expected are only allowed for decide.run",
            file=sys.stderr,
        )
        return 2
    if is_fixture_human_context_command:
        if not args.fixture or not args.anchor_comment_url:
            print(
                "skill_runtime_exec: --fixture and --anchor-comment-url are required for "
                "preflight.run.fixture.with_human_context",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print(
                "skill_runtime_exec: loop flags are not allowed for fixture human context",
                file=sys.stderr,
            )
            return 2
        # Issue #2136 adversarial hardening (H3): validate the ORIGINAL,
        # un-serialized argparse values directly (argv-native), in addition
        # to (not instead of) the exact-parser gate below. The exact parser
        # only sees a re-tokenized `" ".join(...)` + `shlex.split()` round
        # trip of `command_tokens`, which is lossy for any value containing
        # shell-lexer-significant characters -- a value the round trip
        # mis-tokenizes could validate a different (truncated/shifted)
        # substring than the one actually forwarded to `render_command()`
        # and the real child subprocess below. Checking `args.fixture` /
        # `args.investigation_evidence_transport_path` here, before any
        # serialization, closes that validated-value-vs-used-value
        # divergence for this command class.
        if not _is_safe_issue_artifact_path(args.fixture, project_root, str(args.issue_number)):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
        if args.investigation_evidence_transport_path and not _is_safe_issue_artifact_path(
            args.investigation_evidence_transport_path, project_root, str(args.issue_number)
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
        command_tokens = [
            "uv",
            "run",
            "python3",
            SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            args.command_id,
            "--issue-number",
            str(args.issue_number),
            "--repo",
            args.repo,
            "--fixture",
            args.fixture,
            "--anchor-comment-url",
            args.anchor_comment_url,
        ]
        if args.investigation_evidence_transport_path:
            command_tokens.extend(
                [
                    "--investigation-evidence-transport-path",
                    args.investigation_evidence_transport_path,
                ]
            )
        command_text = " ".join(command_tokens)
        if not is_exact_skill_runtime_anchor_fixture_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_fixture_command:
        if not args.fixture:
            print(
                "skill_runtime_exec: --fixture required for preflight.run.fixture", file=sys.stderr
            )
            return 2
        if args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url is not allowed for preflight.run.fixture",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print(
                "skill_runtime_exec: --loop-state-file/--review-result-verdict/"
                "--max-iterations are not allowed for preflight.run.fixture",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--fixture",
                args.fixture,
            ]
        )
        if not is_exact_skill_runtime_fixture_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_anchor_command or is_contract_update_command:
        if args.fixture:
            print(
                "skill_runtime_exec: --fixture is not allowed for anchor runtime commands",
                file=sys.stderr,
            )
            return 2
        if not args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url required for anchor runtime commands",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print(
                "skill_runtime_exec: --loop-state-file/--review-result-verdict/"
                "--max-iterations are not allowed for anchor runtime commands",
                file=sys.stderr,
            )
            return 2
        if args.investigation_evidence_transport_path and args.command_id not in (
            "preflight.run.with_human_context",
            "contract_update.run.with_human_context",
        ):
            print(
                "skill_runtime_exec: --investigation-evidence-transport-path is only "
                "allowed for preflight.run.with_human_context and "
                "contract_update.run.with_human_context",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--anchor-comment-url",
                args.anchor_comment_url,
                *(
                    ["--human-context-comment-url", args.anchor_comment_url]
                    if args.command_id
                    in {
                        "preflight.run.with_human_context",
                        "contract_update.run.with_human_context",
                    }
                    else ["--agent-report-comment-url", args.anchor_comment_url]
                    if args.command_id == "preflight.run.with_agent_report"
                    else []
                ),
                *(
                    [
                        "--investigation-evidence-transport-path",
                        args.investigation_evidence_transport_path,
                    ]
                    if args.investigation_evidence_transport_path
                    else []
                ),
                # Issue #2199 OWNER feedback P1-3: this outer command-string
                # validation gate must see the EXACT same argv shape
                # `render_command()` will actually render below (P2136 H3:
                # validated-value-vs-used-value divergence) -- so this
                # mirrors the SAME condition the render_params block below
                # uses, not an independent one.
                # Issue #2678 AC2: `contract_update.run.with_human_context`
                # (the mutation-phase counterpart of
                # `preflight.run.with_human_context`) receives the SAME
                # executor-internal primary-root propagation, never a
                # caller-suppliable value.
                *(
                    ["--investigation-evidence-primary-root", project_root]
                    if args.investigation_evidence_transport_path
                    and args.command_id
                    in (
                        "preflight.run.with_human_context",
                        "contract_update.run.with_human_context",
                    )
                    else []
                ),
                # Issue #2689 P0-1 fix_delta: this outer command-string
                # validation gate must ALSO see the EXACT same mutation-gate
                # transport triple `render_command()` will actually render
                # below -- mirrors the SAME condition the render_params
                # block below uses (P2136 H3 validated-value-vs-used-value
                # divergence, same precedent as the primary-root propagation
                # above). Only ever non-empty for the two with_human_context
                # command_ids (enforced by the guard above), and only when
                # the caller supplied the full atomic triple.
                *(
                    [
                        "--mutation-category",
                        args.mutation_category,
                        "--owner-user-id",
                        str(args.owner_user_id),
                        "--preview-binding-file",
                        args.preview_binding_file,
                    ]
                    if is_with_human_context_command and args.mutation_category
                    else []
                ),
            ]
        )
        exact_anchor_command = (
            is_exact_skill_runtime_anchor_executor_command(command_text, project_root, project_root)
            if is_anchor_command
            else is_exact_skill_runtime_contract_update_anchor_executor_command(
                command_text, project_root, project_root
            )
        )
        if not exact_anchor_command:
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_decide_command:
        if args.fixture or args.anchor_comment_url:
            print(
                "skill_runtime_exec: --fixture/--anchor-comment-url are not allowed for decide.run",
                file=sys.stderr,
            )
            return 2
        if not args.loop_state_file or not args.review_result_verdict:
            print(
                "skill_runtime_exec: --loop-state-file and --review-result-verdict are required for decide.run",
                file=sys.stderr,
            )
            return 2
        # #2086 P0 fix_delta (Blocker 3): decide.run "Mode B" -- authority
        # sub-fields must be supplied all-or-none (fail-closed on any
        # partial/mixed set), never a generic passthrough of individual
        # optional registry fields. `--authority-expected` is included in
        # this all-or-none set (not treated as independently optional) so a
        # Mode B invocation is always unambiguous about whether a missing/
        # malformed manifest should fail closed.
        _decide_authority_fields = (
            args.invocation_id,
            args.git_head_sha,
            args.authority_transport_path,
        )
        _decide_authority_present = any(_decide_authority_fields) or args.authority_expected
        _decide_authority_complete = all(_decide_authority_fields) and args.authority_expected
        is_decide_authority_mode = False
        if _decide_authority_present:
            if not _decide_authority_complete:
                print(
                    "skill_runtime_exec: decide.run Mode B requires "
                    "--invocation-id, --git-head-sha, --authority-transport-path, "
                    "and --authority-expected all together (all-or-none)",
                    file=sys.stderr,
                )
                return 2
            is_decide_authority_mode = True
        max_iterations = args.max_iterations or "3"
        command_tokens = [
            "uv",
            "run",
            "python3",
            SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            args.command_id,
            "--issue-number",
            str(args.issue_number),
            "--repo",
            args.repo,
            "--loop-state-file",
            args.loop_state_file,
            "--review-result-verdict",
            args.review_result_verdict,
            "--max-iterations",
            max_iterations,
        ]
        if is_decide_authority_mode:
            # These represent skill_runtime_exec.py's OWN new CLI flags on
            # the OUTER invocation (matched by the exact parser below) --
            # not a second --issue-number/--repo pair. The existing
            # --issue-number/--repo already parsed above are the ones
            # forwarded to decide_next_loop_action.py's own authority
            # sub-fields via render_params below.
            command_tokens += [
                "--authority-transport-path",
                args.authority_transport_path,
                "--authority-expected",
                "--invocation-id",
                args.invocation_id,
                "--git-head-sha",
                args.git_head_sha,
            ]
        command_text = " ".join(command_tokens)
        if is_decide_authority_mode:
            exact_decide_command = is_exact_skill_runtime_decide_authority_executor_command(
                command_text, project_root, project_root
            )
        else:
            exact_decide_command = is_exact_skill_runtime_decide_executor_command(
                command_text, project_root, project_root
            )
        if not exact_decide_command:
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_produce_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
        ):
            print(
                "skill_runtime_exec: only --invocation-id/--git-head-sha/"
                "--evidence-fixture-path are allowed for authority_transport.produce",
                file=sys.stderr,
            )
            return 2
        if not args.invocation_id or not args.git_head_sha or not args.evidence_fixture_path:
            print(
                "skill_runtime_exec: --invocation-id, --git-head-sha, and "
                "--evidence-fixture-path are required for authority_transport.produce",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--invocation-id",
                args.invocation_id,
                "--git-head-sha",
                args.git_head_sha,
                "--produce-authority-transport",
                args.evidence_fixture_path,
            ]
        )
        produce_rejection = authority_transport_produce_rejection_reason(
            command_text, os.getcwd(), project_root
        )
        if produce_rejection is not None:
            print(
                f"skill_runtime_exec: authority_transport.produce: {produce_rejection}",
                file=sys.stderr,
            )
            return 2
    elif is_consume_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
        ):
            print(
                "skill_runtime_exec: only --invocation-id/--git-head-sha/"
                "--router-receipt-path/--contract-patch-plan-file/--anchor-context-file "
                "are allowed for authority_transport.consume",
                file=sys.stderr,
            )
            return 2
        if not args.invocation_id or not args.git_head_sha or not args.router_receipt_path:
            print(
                "skill_runtime_exec: --invocation-id, --git-head-sha, and "
                "--router-receipt-path are required for authority_transport.consume",
                file=sys.stderr,
            )
            return 2
        if bool(args.contract_patch_plan_file) != bool(args.anchor_context_file):
            print(
                "skill_runtime_exec: --contract-patch-plan-file and "
                "--anchor-context-file must be supplied together or not at all "
                "for authority_transport.consume",
                file=sys.stderr,
            )
            return 2
        consume_tail = [
            "uv",
            "run",
            "python3",
            SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            args.command_id,
            "--issue-number",
            str(args.issue_number),
            "--repo",
            args.repo,
            "--invocation-id",
            args.invocation_id,
            "--git-head-sha",
            args.git_head_sha,
            "--consume-authority-transport",
            args.router_receipt_path,
        ]
        if args.contract_patch_plan_file and args.anchor_context_file:
            consume_tail += [
                "--contract-patch-plan-file",
                args.contract_patch_plan_file,
                "--anchor-context-file",
                args.anchor_context_file,
            ]
        command_text = " ".join(consume_tail)
        if not is_exact_skill_runtime_authority_transport_consume_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_repair_action_apply_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
        ):
            print(
                "skill_runtime_exec: only --apply-repair-action is allowed for repair_action.apply",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--apply-repair-action",
                args.apply_repair_action,
            ]
        )
        if not is_exact_skill_runtime_repair_action_apply_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_structural_repair_action_apply_command:
        # Preserve strict outer-transport isolation. The safe path predicate is
        # applied to the original argparse value, then the exact parser checks
        # the fixed command shape. No structural payload is opened at root.
        if not _is_safe_issue_artifact_path(
            args.apply_structural_repair_action, project_root, str(args.issue_number)
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--apply-structural-repair-action",
                args.apply_structural_repair_action,
            ]
        )
        if not is_exact_skill_runtime_structural_repair_action_apply_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_owner_reaction_decide_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
            # PR #2694 review fix_delta (P1-1): without this, argparse would
            # accept this flag, the outer command-string reconstruction
            # below would silently drop it (never forwarded to the exact
            # parser or render_params), and the caller-supplied value would
            # be silently ignored rather than rejected.
            or args.investigation_evidence_transport_path
        ):
            print(
                "skill_runtime_exec: only --owner-user-id/--preview-binding-file are allowed for "
                "owner_reaction.decide",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--owner-user-id",
                str(args.owner_user_id),
                "--preview-binding-file",
                args.preview_binding_file,
            ]
        )
        if not is_exact_skill_runtime_owner_reaction_decide_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_owner_reaction_decide_fixture_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
            # PR #2694 review fix_delta (P1-1): same forbidden-flag closure
            # as the production branch above.
            or args.investigation_evidence_transport_path
        ):
            print(
                "skill_runtime_exec: only --owner-user-id/--preview-binding-file/--gh-fixture-file are "
                "allowed for owner_reaction.decide.fixture",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--owner-user-id",
                str(args.owner_user_id),
                "--preview-binding-file",
                args.preview_binding_file,
                "--gh-fixture-file",
                args.gh_fixture_file,
            ]
        )
        if not is_exact_skill_runtime_owner_reaction_decide_fixture_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    else:
        if args.fixture:
            print(
                "skill_runtime_exec: --fixture is only allowed for preflight.run.fixture",
                file=sys.stderr,
            )
            return 2
        if args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url is only allowed for an anchor-bound preflight profile",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print(
                "skill_runtime_exec: --loop-state-file/--review-result-verdict/"
                "--max-iterations are only allowed for decide.run",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
            ]
        )
        if not is_exact_skill_runtime_executor_command(command_text, project_root, project_root):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2

    is_production_dedicated_command = args.command_id in PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS

    _validate_runtime_context(project_root, args)
    entry = load_registry_entry(args.command_id, project_root)
    validate_registry_entry(args.command_id, entry, str(args.issue_number))

    registry_path = Path(project_root) / REGISTRY_REL
    if registry_path.is_symlink():
        raise RuntimeError("registry_symlink_not_allowed")
    if not registry_path.is_file():
        raise RuntimeError("registry_missing")

    # #2086 AC10: decide.run dispatches decide_next_loop_action.py, not
    # run_refinement_preflight.py -- the pre-existing integrity/symlink
    # check below must validate the script the command_id actually reaches,
    # otherwise decide.run could never pass this check even though it never
    # touches run_refinement_preflight.py.
    #
    # Issue #2311 AC1: canonical bare `preflight.run` now first-hops into
    # `workflow_start_entry.py` (not `run_refinement_preflight.py` directly).
    # Sibling anchor-comment-driven profiles (`preflight.run.with_anchor` /
    # `.with_human_context` / `.with_agent_report` / `.fixture` /
    # `.fixture.with_human_context`) are unaffected by this Issue and
    # continue to first-hop into `run_refinement_preflight.py`. This is a
    # three-way branch (not a generalized/loosened check): each branch is an
    # exact, explicit first-hop target for its command_id class.
    if is_decide_command:
        script_name = "decide_next_loop_action.py"
    elif args.command_id == "preflight.run":
        script_name = "workflow_start_entry.py"
    elif is_owner_reaction_decide_command or is_owner_reaction_decide_fixture_command:
        # Issue #2688 AC5: the first-hop script integrity/readback target
        # must match the actual first-hop script command_registry.py
        # renders for owner_reaction.decide/.fixture -- `owner_reaction_
        # decision.py`, NOT `run_refinement_preflight.py` (the `else`
        # branch below's target for every other command_id here).
        script_name = "owner_reaction_decision.py"
    else:
        script_name = "run_refinement_preflight.py"
    script_path = (
        Path(project_root)
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / script_name
    )
    if script_path.is_symlink() or not script_path.is_file():
        raise RuntimeError("preflight_script_invalid")

    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("issue_refinement_command_registry_executor", str(registry_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("registry_spec_invalid")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    render_command = getattr(module, "render_command", None)
    if not callable(render_command):
        raise RuntimeError("render_command_missing")  # noqa: TRY004
    # #2086 AC10 P0: `decide.run`'s registry entry declares only
    # loop_state_file/verdict/max_iterations placeholders (it has no
    # `{issue_number}`/`{repo}` tokens in its argv template -- unlike every
    # other command_id here). render_command() fail-closed-rejects any
    # extra params not in the entry's declared `placeholders`, so
    # unconditionally seeding `issue_number`/`repo` into render_params
    # made every real decide.run dispatch raise `ValueError` before ever
    # reaching a subprocess. This was masked by the previous test's stub
    # `render_command()`, which silently ignored undeclared params instead
    # of validating them (see test_decide_run_reaches_real_subprocess).
    if is_decide_command:
        render_params: dict[str, object] = {
            "loop_state_file": args.loop_state_file,
            "verdict": args.review_result_verdict,
            "max_iterations": args.max_iterations or "3",
        }
        # #2086 P0 fix_delta (Blocker 3): Mode B -- forward the authority
        # sub-fields to decide_next_loop_action.py's own
        # --issue-number/--repo/--authority-transport-path/
        # --authority-expected/--invocation-id/--git-head-sha flags
        # (command_registry.py decide.run entry, PR #2068). Only populated
        # when the pre-dispatch all-or-none check above accepted a
        # complete Mode B field set.
        if is_decide_authority_mode:
            render_params["issue_number"] = args.issue_number
            render_params["repo"] = args.repo
            render_params["authority_transport_manifest_path"] = args.authority_transport_path
            render_params["authority_expected"] = True
            render_params["invocation_id"] = args.invocation_id
            render_params["git_head_sha"] = args.git_head_sha
    elif is_produce_command:
        # #2086 AC9/AC10: producer role -- issue_number/repo are NOT seeded
        # here (unlike the generic `else` branch below) because
        # `authority_transport.produce`'s own render_params below already
        # supplies them alongside its own required placeholders.
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "invocation_id": args.invocation_id,
            "git_head_sha": args.git_head_sha,
            "evidence_fixture_path": args.evidence_fixture_path,
        }
    elif is_consume_command:
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "invocation_id": args.invocation_id,
            "git_head_sha": args.git_head_sha,
            "router_receipt_path": args.router_receipt_path,
        }
        if args.contract_patch_plan_file and args.anchor_context_file:
            render_params["contract_patch_plan_file"] = args.contract_patch_plan_file
            render_params["anchor_context_file"] = args.anchor_context_file
    elif is_repair_action_apply_command:
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "preflight_result_path": args.apply_repair_action,
        }
    elif is_structural_repair_action_apply_command:
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "preflight_result_path": args.apply_structural_repair_action,
        }
    elif is_owner_reaction_decide_command:
        # Issue #2688 AC2: production profile shares the SAME registry
        # entry / render_command() dispatch as the fixture sibling below --
        # only `gh_fixture_file` differs.
        render_params = {
            "repo": args.repo,
            "issue_number": args.issue_number,
            "owner_user_id": args.owner_user_id,
            "preview_binding_file": args.preview_binding_file,
        }
    elif is_owner_reaction_decide_fixture_command:
        render_params = {
            "repo": args.repo,
            "issue_number": args.issue_number,
            "owner_user_id": args.owner_user_id,
            "preview_binding_file": args.preview_binding_file,
            "gh_fixture_file": args.gh_fixture_file,
        }
    else:
        render_params = {"issue_number": args.issue_number, "repo": args.repo}
        if is_fixture_command or is_fixture_human_context_command:
            render_params["fixture"] = args.fixture
        if is_anchor_command or is_contract_update_command or is_fixture_human_context_command:
            render_params["anchor_comment_url"] = args.anchor_comment_url
            if args.investigation_evidence_transport_path:
                render_params["investigation_evidence_transport_path"] = (
                    args.investigation_evidence_transport_path
                )
                # Issue #2199 OWNER feedback P1-3: `preflight.run.with_human_context`
                # is one of the 4 production preflight profiles -- its child
                # always dispatches under the #2197/#2199 dedicated worktree
                # (`execution_root`, below), never `project_root`. The
                # transport path was validated above as relative to
                # `project_root` (the PRIMARY checkout); without this
                # explicit handoff of that SAME `project_root`, the child's
                # own `_find_repo_root()` (derived from ITS `__file__`,
                # under the dedicated worktree once dispatched there) would
                # resolve the identical relative string against a different
                # root and silently miss the real evidence file. Always the
                # executor's OWN already-verified `project_root` -- never a
                # caller-suppliable value.
                # Issue #2678 AC2: extend the SAME executor-internal
                # primary-root propagation to the mutation-phase
                # `contract_update.run.with_human_context` profile (never a
                # caller-supplied root).
                if args.command_id in (
                    "preflight.run.with_human_context",
                    "contract_update.run.with_human_context",
                ):
                    render_params["investigation_evidence_primary_root"] = project_root
            # Issue #2689 P0-1 fix_delta: the mutation-gate transport triple
            # -- ONLY ever populated for the two with_human_context
            # command_ids (guarded above), and ONLY when the caller
            # supplied the full atomic triple (`args.mutation_category`
            # truthy implies `args.owner_user_id`/`args.preview_binding_file`
            # are also present, per the all-or-none guard above).
            if is_with_human_context_command and args.mutation_category:
                render_params["mutation_category"] = args.mutation_category
                render_params["owner_user_id"] = args.owner_user_id
                render_params["preview_binding_file"] = args.preview_binding_file
    child_argv = render_command(args.command_id, render_params)
    child_argv = _resolve_child_argv(child_argv)

    timeout_seconds = entry.get("timeout_seconds")
    env = _sanitize_env(project_root, args.command_id)
    binary_output = is_structural_repair_action_apply_command

    if not is_production_dedicated_command:
        # Fixture profiles (`preflight.run.fixture` /
        # `preflight.run.fixture.with_human_context`) -- along with every
        # other command_id not in `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`
        # -- are unaffected by Issue #2199/#2393 and keep dispatching at
        # `project_root` exactly as before (AC8/#2199 non-regression).
        # `contract_update.run.with_anchor`/`.with_human_context` used to be
        # excluded here too (#2199 AC8); Issue #2393 moved them into
        # `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`, so they now take the
        # dedicated-runtime branch below instead of this one.
        return _dispatch_child_and_check_postconditions(
            dispatch_root=project_root,
            issue_number=args.issue_number,
            command_id=args.command_id,
            child_argv=child_argv,
            env=env,
            timeout_seconds=timeout_seconds,
            binary_output=binary_output,
            repo=args.repo,
        )

    # Issue #2199/#2393: the 4 production preflight profiles plus the 2
    # contract_update mutation profiles
    # (`PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`) dispatch their child
    # under the #2197 dedicated worktree (`execution_root`) instead of
    # `project_root`, with the fixed #2198 lifecycle guard held across
    # bootstrap, dedicated-identity verification, child dispatch, and the
    # SAME post-child checks retargeted at `execution_root` (AC3/AC9) --
    # never a primary-root fallback on any dedicated-identity failure
    # (AC11). `capture_primary_checkout_invariant_snapshot()` is an
    # unconditional stop condition (Issue #2199 OWNER feedback P2): it
    # fails closed if the PRIMARY checkout's observable state (including,
    # for already-dirty tracked paths, their own content) differs at all
    # across the span, on both the success and the failure path (AC1) --
    # it does not attribute an observed difference to this dispatch's own
    # child specifically (see that function's own docstring).
    bootstrap_mod = _load_worktree_bootstrap_exec_module()
    primary_before = capture_primary_checkout_invariant_snapshot(project_root)
    exit_code: int | None = None
    raised_exc: BaseException | None = None
    try:
        with bootstrap_mod.control_plane_dedicated_execution_session(project_root) as session:
            execution_root = str(session["execution_root"])
            executor_script_path = str(
                Path(execution_root)
                / ".claude"
                / "skills"
                / "issue-refinement-loop"
                / "scripts"
                / script_name
            )
            bootstrap_mod.verify_dedicated_control_plane_identity(
                session,
                project_root=project_root,
                execution_root=execution_root,
                invocation_cwd=execution_root,
                executor_script_path=executor_script_path,
            )
            # Issue #2199 OWNER feedback P1-1: the dedicated worktree's own
            # `uv`-managed project environment is relocated OUTSIDE the
            # monitored `execution_root` tree via `UV_PROJECT_ENVIRONMENT`
            # (`bootstrap_mod.dedicated_execution_venv_dir`), and explicitly
            # prepared (`ensure_dedicated_execution_environment_ready`)
            # BEFORE `_dispatch_child_and_check_postconditions` takes its
            # before-snapshot -- so neither a first-run `.venv` creation nor
            # a first-sync `uv.lock` mtime touch can ever appear inside the
            # write-monitoring window below. `dedicated_env` (not the outer
            # `env`) is what actually reaches the monitored child dispatch,
            # so the child observes the SAME relocated environment this
            # preparation step just readied.
            dedicated_env = dict(env)
            # Issue #2393 AC4: a caller-supplied relative `GH_CONFIG_DIR`
            # (carried through by `_sanitize_env()`'s
            # `gh_config_dir_carrier_command_ids` allowlist, which already
            # includes both `contract_update.run.*` profiles) is anchored to
            # the INVOCATION cwd (`project_root` -- `_validate_runtime_context()`
            # above already required `os.getcwd() == project_root`) BEFORE
            # the child's own cwd switches to `execution_root` below.
            # Without this, a relative value would silently resolve against
            # the WRONG directory once the child's cwd differs from the
            # invocation cwd (a gap that did not exist before this dispatch
            # became dedicated-worktree-routed, since dispatch_root ==
            # project_root == invocation cwd for every non-dedicated
            # command_id). Generalizes the same anchoring PR #2407 already
            # relies on for bare `preflight.run`'s dedicated dispatch. An
            # absolute value is never modified. No token copying, no
            # config-content reads, no allowlist widening.
            raw_gh_config_dir = dedicated_env.get("GH_CONFIG_DIR")
            if raw_gh_config_dir and not os.path.isabs(raw_gh_config_dir):
                dedicated_env["GH_CONFIG_DIR"] = os.path.realpath(
                    os.path.join(project_root, raw_gh_config_dir)
                )
            # An unmanaged project (`[tool.uv].managed = false`) never
            # triggers `uv`'s own environment sync/`.venv` creation at all
            # (`uv sync`/`uv run` refuse to manage it by design) -- it was
            # never exposed to the P1-1 misclassification this relocation
            # exists to prevent, so this step is skipped entirely for it
            # (never a hard failure), preserving byte-identical pre-#2199
            # behavior for every unmanaged-project caller/fixture.
            if _is_managed_uv_project(execution_root):
                dedicated_env["UV_PROJECT_ENVIRONMENT"] = (
                    bootstrap_mod.dedicated_execution_venv_dir(project_root)
                )
                bootstrap_mod.ensure_dedicated_execution_environment_ready(
                    execution_root=execution_root,
                    uv_executable=child_argv[0],
                    env=dedicated_env,
                )
            exit_code = _dispatch_child_and_check_postconditions(
                dispatch_root=execution_root,
                issue_number=args.issue_number,
                command_id=args.command_id,
                child_argv=child_argv,
                env=dedicated_env,
                timeout_seconds=timeout_seconds,
                binary_output=binary_output,
                repo=args.repo,
            )
    except BaseException as exc:  # noqa: BLE001 -- re-raised below unless the primary-checkout guard below fires
        raised_exc = exc
    primary_after = capture_primary_checkout_invariant_snapshot(project_root)
    if primary_after != primary_before:
        return _emit_primary_checkout_drift_failure(args.issue_number)
    if raised_exc is not None:
        raise raised_exc
    return exit_code


# ---------------------------------------------------------------------------
# Issue #2378: closed remote-default-ref Git protocol builders. A consumer can
# express only protocol data flow; it cannot provide argv, flags, a refspec,
# a worktree action, or a lock reason.
# ---------------------------------------------------------------------------

_GIT_SUBPROCESS_EXECUTABLE_CACHE: str | None = None
_GIT_NO_LAZY_FETCH_CAPABILITY_CACHE: dict[str, bool] = {}
_CONTROL_PLANE_WORKTREE_LOCK_REASON = "agent-platform-control-plane-default-ref"

# The short-lived per-invocation supervisor, never the long-lived executor
# host, opts into PR_SET_CHILD_SUBREAPER. Consequently an orphaned Git
# descendant is reparented only to the supervisor that spawned its Git leader;
# the host's unrelated children cannot enter this attribution domain.
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _enable_linux_child_subreaper() -> bool:
    """Enable containment in an invocation supervisor before it spawns Git.

    This function is called exclusively after `_run_closed_git_process` has
    forked its short-lived supervisor. It must never run in the long-lived
    executor host: PR_SET_CHILD_SUBREAPER changes orphan reparenting semantics
    for the entire calling process and would otherwise make unrelated host
    children eligible for classification, signalling, or reaping.

    The supervisor remains alive until its one Git invocation reaches a
    confirmed terminal state, so an escaped descendant is attributable to the
    known Git leader rather than to the host. This containment implementation
    is deliberately Linux/WSL-only and fail-closed on other platforms; it runs
    only in the short-lived single-threaded supervisor after `fork()`.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        enabled = ctypes.c_int()
        if prctl(_PR_GET_CHILD_SUBREAPER, ctypes.addressof(enabled), 0, 0, 0) != 0:
            return False
        if enabled.value:
            return True
        if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            return False
        confirmed = ctypes.c_int()
        return (
            prctl(_PR_GET_CHILD_SUBREAPER, ctypes.addressof(confirmed), 0, 0, 0) == 0
            and confirmed.value == 1
        )
    except (AttributeError, OSError):
        return False


class GitProtocolDeadlineExhausted(RuntimeError):
    """Raised before a terminal command could consume cleanup reserve."""


class GitProtocolTimeout(RuntimeError):
    """Raised only after a containment backend confirms terminal cleanup."""


class GitProtocolProcessGroupCleanupFailed(RuntimeError):
    """Fail closed when dedicated process-group absence is not confirmed."""


class ControlPlaneUnavailable(RuntimeError):
    """Fail-closed terminal state for the control-plane remote-binding and
    fixed-worktree-recovery protocols (Issue #2197 AC3/AC4/AC5/AC7).

    Raised for: a rejected S1/fetch/F/S2 remote-binding state machine (a
    changed symbolic target, or an `S2` that does not corroborate `F`), a
    failed builder-owned CAS cleanup of the fetched private ref, or a fixed
    dedicated worktree recovery state that cannot be proven safe (dirty
    working tree, unknown owner, or git-common-dir linkage mismatch). This
    type is never used to report an ordinary programming error -- only a
    protocol precondition this module deliberately refuses to proceed past
    without positive proof.
    """


# Issue #2197 AC1: the sole production remote authority. Never sourced from a
# caller argument, environment variable, or the local repository's `origin`
# remote configuration (which may itself be SSH) -- `origin` is never read by
# any control-plane remote-binding operation. Built from the already-trusted,
# single repository-identity source (`repo_identity`) so this file introduces no second
# repository-identity literal.
try:
    CONTROL_PLANE_CANONICAL_REMOTE_URL = f"https://github.com/{load_trusted_repo_slug()}.git"
except RepositoryIdentityError:
    # Unusable identity config: an empty URL is rejected by validate_literal_remote_url (fail-closed).
    CONTROL_PLANE_CANONICAL_REMOTE_URL = ""


@dataclass(frozen=True)
class GitProtocolDeadline:
    """One monotonic deadline shared by every step in one remote protocol."""

    deadline_at: float
    cleanup_reserve_seconds: float

    @classmethod
    def start(
        cls, timeout_seconds: float, cleanup_reserve_seconds: float = 1.0
    ) -> GitProtocolDeadline:
        timeout = _validate_deadline_value(timeout_seconds, "timeout")
        cleanup_reserve = _validate_deadline_value(cleanup_reserve_seconds, "cleanup_reserve")
        if timeout <= cleanup_reserve:
            raise ValueError("git_protocol_deadline_invalid")
        return cls(time.monotonic() + timeout, cleanup_reserve)

    def execution_seconds(self) -> float:
        deadline_at = _validate_deadline_value(self.deadline_at, "deadline_at")
        cleanup_reserve = _validate_deadline_value(self.cleanup_reserve_seconds, "cleanup_reserve")
        remaining = deadline_at - time.monotonic()
        if remaining <= cleanup_reserve:
            raise GitProtocolDeadlineExhausted("git_protocol_cleanup_reserve_required")
        return remaining - cleanup_reserve


def resolve_git_subprocess_executable(project_root: str) -> str:
    global _GIT_SUBPROCESS_EXECUTABLE_CACHE
    if _GIT_SUBPROCESS_EXECUTABLE_CACHE is None:
        _GIT_SUBPROCESS_EXECUTABLE_CACHE = _resolve_trusted_executable("git", project_root)
    return _GIT_SUBPROCESS_EXECUTABLE_CACHE


def _reset_git_subprocess_executable_cache_for_tests() -> None:
    global _GIT_SUBPROCESS_EXECUTABLE_CACHE
    _GIT_SUBPROCESS_EXECUTABLE_CACHE = None
    _GIT_NO_LAZY_FETCH_CAPABILITY_CACHE.clear()


def _resolve_trusted_ssh_command() -> str:
    ssh_path = shutil.which("ssh", path=os.pathsep.join(_safe_path_entries()))
    return f"{ssh_path} -oBatchMode=yes -oStrictHostKeyChecking=yes" if ssh_path else "false"


def sanitized_git_subprocess_env(project_root: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if key not in GIT_SUBPROCESS_UNSET_ENV_KEYS
    }
    env["CLAUDE_PROJECT_DIR"] = project_root
    env["PATH"] = os.pathsep.join(_safe_path_entries())
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GIT_SSH_COMMAND"] = _resolve_trusted_ssh_command()
    return env


def git_subprocess_trusted_hooks_dir(scratch_root: str) -> str:
    scratch_path = Path(scratch_root)
    if not scratch_path.is_absolute() or _is_symlink_path(scratch_path):
        raise RuntimeError("git_subprocess_scratch_root_invalid")
    scratch_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = Path(tempfile.mkdtemp(prefix=".skill-runtime-git-hooks-", dir=str(scratch_path)))
    if Path(os.path.realpath(hooks_dir)) != hooks_dir or any(hooks_dir.iterdir()):
        raise RuntimeError("git_subprocess_trusted_hooks_dir_invalid")
    return str(hooks_dir)


@dataclass(frozen=True)
class _GitOperation:
    """Private supported operation plus typed payload, never caller argv."""

    kind: str
    remote_url: LiteralRemoteUrl | None = None
    remote_ref: AllowedRemoteRef | None = None
    private_ref: ControlPlanePrivateRef | None = None
    object_id: RepositoryObjectId | None = None
    worktree_path: DetachedWorktreePath | None = None


_SUPPORTED_GIT_OPERATION_KINDS = frozenset(
    {
        "probe_rewrite",
        "probe_no_lazy_fetch_support",
        "probe_promisor_remote",
        "effective_remote_url",
        "observe_default_ref",
        "repository_object_format",
        "fetch_default_ref",
        "fetch_default_ref_no_lazy",
        "read_private_ref_oid",
        "require_commit_object",
        "read_worktree_head",
        "read_worktree_status_porcelain",
        "add_detached_locked_worktree",
        "remove_detached_locked_worktree",
        "list_worktrees_porcelain",
        "list_worktrees_porcelain_locked_prunable",
        "delete_private_ref_cas",
    }
)


def _validate_deadline_value(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"git_protocol_deadline_{field}_invalid")  # noqa: TRY004
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"git_protocol_deadline_{field}_invalid")
    return normalized


def _revalidate_literal_remote_url(value: LiteralRemoteUrl) -> LiteralRemoteUrl:
    if not isinstance(value, LiteralRemoteUrl):
        raise TypeError("literal_remote_url_required")
    return validate_literal_remote_url(value.value)


def _revalidate_allowed_remote_ref(value: AllowedRemoteRef) -> AllowedRemoteRef:
    if not isinstance(value, AllowedRemoteRef):
        raise TypeError("allowed_remote_ref_required")
    return validate_allowed_remote_ref(value.value)


def _revalidate_private_ref(value: ControlPlanePrivateRef) -> ControlPlanePrivateRef:
    if not isinstance(value, ControlPlanePrivateRef):
        raise TypeError("control_plane_private_ref_required")
    return validate_control_plane_private_ref(value.value)


def _revalidate_object_format(value: RepositoryObjectFormat) -> RepositoryObjectFormat:
    if not isinstance(value, RepositoryObjectFormat):
        raise TypeError("repository_object_format_required")
    return validate_repository_object_format(value.value)


def _revalidate_object_id(
    value: RepositoryObjectId, object_format: RepositoryObjectFormat | None = None
) -> RepositoryObjectId:
    if not isinstance(value, RepositoryObjectId):
        raise TypeError("repository_object_id_required")
    format_value = object_format
    if format_value is None:
        format_value = validate_repository_object_format(
            "sha1" if len(value.value) == 40 else "sha256"
        )
    return validate_repository_object_id(value.value, _revalidate_object_format(format_value))


def _revalidate_worktree_path(
    value: DetachedWorktreePath, project_root: str, *, require_fresh: bool
) -> DetachedWorktreePath:
    if not isinstance(value, DetachedWorktreePath):
        raise TypeError("detached_worktree_path_required")
    validator = (
        validate_detached_worktree_path
        if require_fresh
        else validate_existing_detached_worktree_path
    )
    return validator(value.value, project_root)


def _revalidate_semantic_operation(operation: _GitOperation, project_root: str) -> _GitOperation:
    """Revalidate path-bearing internal operations before even the rewrite probe.

    `_GitOperation` is private, but it remains a trust boundary: a forged
    `DetachedWorktreePath` must not turn into argv merely because it carries
    the right runtime type.
    """
    if not isinstance(operation, _GitOperation):
        raise TypeError("git_operation_required")
    if operation.kind not in _SUPPORTED_GIT_OPERATION_KINDS:
        raise ValueError("git_operation_not_supported")
    if operation.kind == "add_detached_locked_worktree":
        return _GitOperation(
            operation.kind,
            object_id=operation.object_id,
            worktree_path=_revalidate_worktree_path(
                operation.worktree_path, project_root, require_fresh=True
            ),
        )
    if operation.kind == "remove_detached_locked_worktree":
        return _GitOperation(
            operation.kind,
            worktree_path=_revalidate_worktree_path(
                operation.worktree_path, project_root, require_fresh=False
            ),
        )
    # `fetch_default_ref_no_lazy` is internally synthesized only after the
    # trusted executable passed the fixed capability probe. Rebuild every
    # payload-bearing operation so a forged dataclass cannot smuggle state.
    if operation.kind in {"fetch_default_ref", "fetch_default_ref_no_lazy"}:
        return _GitOperation(
            "fetch_default_ref",
            remote_url=operation.remote_url,
            remote_ref=operation.remote_ref,
            private_ref=operation.private_ref,
        )
    return operation


@dataclass(frozen=True)
class _TrackedGitDescendant:
    """A Linux process identity observed beneath a dedicated Git leader."""

    pid: int
    start_time: str


def _linux_process_identity(pid: int) -> tuple[int, int, str] | None:
    """Read `(pid, parent_pid, start_time)` without trusting a reused PID."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    _prefix, separator, suffix = stat.rpartition(")")
    fields = suffix.split()
    if not separator or len(fields) < 20:
        return None
    try:
        return (pid, int(fields[1]), fields[19])
    except ValueError:
        return None


def _observe_git_descendants(leader_pid: int) -> set[_TrackedGitDescendant] | None:
    """Observe the live descendant tree before a child can escape its group.

    A descendant may call `setsid()` and leave the group created by
    `start_new_session`. Linux `/proc` parent links are an advisory snapshot
    only: an escaped child can fork a delayed descendant and exit between two
    observations. Callers must therefore never use a snapshot as proof that
    all descendants are absent; terminal cleanup remains fail-closed.
    """
    proc_root = Path("/proc")
    if not sys.platform.startswith("linux") or not proc_root.is_dir():
        return None
    records: dict[int, tuple[int, str]] = {}
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in proc_entries:
        if not entry.name.isdecimal():
            continue
        identity = _linux_process_identity(int(entry.name))
        if identity is not None:
            pid, parent_pid, start_time = identity
            records[pid] = (parent_pid, start_time)
    descendants: set[_TrackedGitDescendant] = set()
    frontier = {leader_pid}
    while frontier:
        parent_pid = frontier.pop()
        children = {
            pid: start_time
            for pid, (record_parent_pid, start_time) in records.items()
            if record_parent_pid == parent_pid
        }
        frontier.update(children)
        descendants.update(
            _TrackedGitDescendant(pid, start_time) for pid, start_time in children.items()
        )
    return descendants


def _tracked_descendants_absent(descendants: set[_TrackedGitDescendant]) -> bool:
    return all(
        (identity := _linux_process_identity(descendant.pid)) is None
        or identity[2] != descendant.start_time
        for descendant in descendants
    )


def _observe_invocation_supervisor_children() -> set[_TrackedGitDescendant] | None:
    """Return live children adopted by this one-invocation supervisor.

    This is deliberately distinct from `_observe_git_descendants`: a readable
    tree snapshot rooted at the Git leader is only advisory. Once that leader
    is reaped, the supervisor's PR_SET_CHILD_SUBREAPER gives an empty
    direct-child set a kernel-parentage meaning: no live orphaned descendant
    of that Git leader remains. The supervisor spawned no other child, and an
    unreadable `/proc` result is never interpreted as that proof.
    """
    proc_root = Path("/proc")
    if not sys.platform.startswith("linux") or not proc_root.is_dir():
        return None
    parent_pid = os.getpid()
    children: set[_TrackedGitDescendant] = set()
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in proc_entries:
        if not entry.name.isdecimal():
            continue
        identity = _linux_process_identity(int(entry.name))
        if identity is not None and identity[1] == parent_pid:
            children.add(_TrackedGitDescendant(identity[0], identity[2]))
    return children


def _observe_descendants_of(parent_pids: set[int]) -> set[_TrackedGitDescendant] | None:
    """Return the complete current tree below known Linux parent PIDs."""
    proc_root = Path("/proc")
    if not sys.platform.startswith("linux") or not proc_root.is_dir():
        return None
    records: dict[int, tuple[int, str]] = {}
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in proc_entries:
        if not entry.name.isdecimal():
            continue
        identity = _linux_process_identity(int(entry.name))
        if identity is not None:
            records[identity[0]] = (identity[1], identity[2])
    descendants: set[_TrackedGitDescendant] = set()
    frontier = set(parent_pids)
    while frontier:
        parent_pid = frontier.pop()
        children = {
            pid: start_time
            for pid, (record_parent_pid, start_time) in records.items()
            if record_parent_pid == parent_pid
        }
        frontier.update(children)
        descendants.update(
            _TrackedGitDescendant(pid, start_time) for pid, start_time in children.items()
        )
    return descendants


def _signal_invocation_child_trees(
    children: set[_TrackedGitDescendant], signal_number: int
) -> bool:
    descendants = _observe_descendants_of({child.pid for child in children})
    if descendants is None:
        return False
    return _signal_tracked_descendants(children | descendants, signal_number)


def _reap_tracked_children(children: set[_TrackedGitDescendant]) -> bool:
    """Reap only verified direct subreaper children; never a reused PID."""
    for child in children:
        identity = _linux_process_identity(child.pid)
        if identity is None or identity[2] != child.start_time:
            continue
        try:
            os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError:
            # It was reaped by a concurrent wait; it is no longer a leak.
            continue
        except OSError:
            return False
    return True


def _terminate_invocation_supervisor_children(deadline: GitProtocolDeadline) -> bool:
    """Drain Git descendants adopted by this invocation supervisor only.

    A direct child of the supervisor may itself fork after SIGTERM. Repeating
    discovery is not used as a timing heuristic: its parentage is still within
    the known Git invocation. No host child can be seen by this process.
    """
    term_until = min(
        deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2)
    )
    for signal_number, phase_deadline in (
        (signal.SIGTERM, term_until),
        (signal.SIGKILL, deadline.deadline_at),
    ):
        while time.monotonic() < phase_deadline:
            children = _observe_invocation_supervisor_children()
            if children is None:
                return False
            if not children:
                return True
            if not _signal_invocation_child_trees(children, signal_number):
                return False
            if not _reap_tracked_children(children):
                return False
            time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    children = _observe_invocation_supervisor_children()
    if children is None:
        return False
    if children and not _signal_invocation_child_trees(children, signal.SIGKILL):
        return False
    if children and not _reap_tracked_children(children):
        return False
    final_children = _observe_invocation_supervisor_children()
    return final_children is not None and not final_children


def _signal_tracked_descendants(
    descendants: set[_TrackedGitDescendant], signal_number: int
) -> bool:
    """Signal only descendants whose PID identity still matches observation."""
    for descendant in descendants:
        identity = _linux_process_identity(descendant.pid)
        if identity is None or identity[2] != descendant.start_time:
            continue
        try:
            os.kill(descendant.pid, signal_number)
        except ProcessLookupError:
            continue
        except OSError:
            return False
    return True


def _terminate_git_process_group(
    proc: subprocess.Popen,
    pgid: int | None,
    deadline: GitProtocolDeadline,
    descendants: set[_TrackedGitDescendant] | None,
) -> bool:
    """Bounded cleanup with supervisor-scoped containment confirmation.

    The caller is the invocation supervisor. Its subreaper domain contains
    only the Git leader it spawned and descendants subsequently orphaned from
    that leader, never a child of the long-lived executor host.
    """
    if not _POSIX_PROCESS_GROUP_SUPPORTED or pgid is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.0, deadline.deadline_at - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=max(0.0, deadline.deadline_at - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                pass
        return False

    if descendants is None:
        # Observation itself failed after Popen. We cannot confirm containment,
        # but the known dedicated group must still receive bounded TERM/KILL.
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        term_until = min(
            deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2)
        )
        while time.monotonic() < term_until:
            if _verify_process_group_absent(pgid):
                try:
                    proc.wait(timeout=0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                _terminate_invocation_supervisor_children(deadline)
                return False
            time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        while time.monotonic() < deadline.deadline_at:
            if _verify_process_group_absent(pgid):
                try:
                    proc.wait(timeout=0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                _terminate_invocation_supervisor_children(deadline)
                return False
            time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
        _terminate_invocation_supervisor_children(deadline)
        return False

    def confirmed_absence() -> bool:
        children = _observe_invocation_supervisor_children()
        if children is None:
            return False
        if children:
            # An escaped descendant was observed in the isolated supervisor.
            # Reap it, but do not promote an escape into successful cleanup.
            _terminate_invocation_supervisor_children(deadline)
            return False
        return True

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    if not _signal_tracked_descendants(descendants, signal.SIGTERM):
        return False
    term_until = min(
        deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2)
    )
    while time.monotonic() < term_until:
        proc.poll()
        if _verify_process_group_absent(pgid) and _tracked_descendants_absent(descendants):
            try:
                proc.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return confirmed_absence()
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    if not _signal_tracked_descendants(descendants, signal.SIGKILL):
        return False
    while time.monotonic() < deadline.deadline_at:
        proc.poll()
        if _verify_process_group_absent(pgid) and _tracked_descendants_absent(descendants):
            try:
                proc.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return confirmed_absence()
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    # Ensure only invocation-attributable adopted children are not left as
    # zombies, even though final confirmation has failed.
    _terminate_invocation_supervisor_children(deadline)
    return False


def _operation_arguments(operation: _GitOperation, *, cwd: str) -> list[str]:
    """Construct exact argv only from a supported operation and typed payload."""
    if (
        not isinstance(operation, _GitOperation)
        or operation.kind not in _SUPPORTED_GIT_OPERATION_KINDS
    ):
        raise ValueError("git_operation_not_supported")
    if operation.kind == "probe_rewrite":
        return [
            "-C",
            cwd,
            "config",
            "--get-regexp",
            "--name-only",
            "-z",
            INSTEADOF_CONFIG_NAME_REGEXP,
        ]
    if operation.kind == "probe_no_lazy_fetch_support":
        return ["--version"]
    if operation.kind == "probe_promisor_remote":
        return ["config", "--local", "--get-regexp", r"^remote\..*\.promisor$"]
    if operation.kind == "effective_remote_url":
        remote_url = _revalidate_literal_remote_url(operation.remote_url)
        return ["ls-remote", "--get-url", remote_url.value]
    if operation.kind == "observe_default_ref":
        remote_url = _revalidate_literal_remote_url(operation.remote_url)
        return ["ls-remote", "--exit-code", "--symref", remote_url.value, "HEAD"]
    if operation.kind == "repository_object_format":
        return ["rev-parse", "--show-object-format"]
    if operation.kind in {"fetch_default_ref", "fetch_default_ref_no_lazy"}:
        remote_url = _revalidate_literal_remote_url(operation.remote_url)
        remote_ref = _revalidate_allowed_remote_ref(operation.remote_ref)
        private_ref = _revalidate_private_ref(operation.private_ref)
        return [
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            remote_url.value,
            f"{remote_ref.value}:{private_ref.value}",
        ]
    if operation.kind == "read_private_ref_oid":
        private_ref = _revalidate_private_ref(operation.private_ref)
        return ["rev-parse", "--verify", "--quiet", private_ref.value]
    if operation.kind == "require_commit_object":
        object_id = _revalidate_object_id(operation.object_id)
        return ["cat-file", "-e", object_id.value + "^{commit}"]
    if operation.kind == "read_worktree_head":
        return ["rev-parse", "--verify", "HEAD"]
    if operation.kind == "read_worktree_status_porcelain":
        return ["status", "--porcelain", "--untracked-files=all"]
    if operation.kind == "add_detached_locked_worktree":
        path = operation.worktree_path
        object_id = _revalidate_object_id(operation.object_id)
        if not isinstance(path, DetachedWorktreePath):
            raise TypeError("detached_worktree_path_required")
        return [
            "worktree",
            "add",
            "--detach",
            "--lock",
            "--reason",
            _CONTROL_PLANE_WORKTREE_LOCK_REASON,
            path.value,
            object_id.value,
        ]
    if operation.kind == "remove_detached_locked_worktree":
        path = operation.worktree_path
        if not isinstance(path, DetachedWorktreePath):
            raise TypeError("detached_worktree_path_required")
        return ["worktree", "remove", "--force", "--force", path.value]
    if operation.kind == "list_worktrees_porcelain":
        return ["worktree", "list", "--porcelain"]
    if operation.kind == "list_worktrees_porcelain_locked_prunable":
        # Issue #2199 AC2/AC10/AC12: minimal typed extension to the closed
        # git-worktree-listing seam that additionally exposes `locked`/
        # `prunable` porcelain fields (the `-z` form matches the parsing
        # convention already used by `worktree_catalog.py`). Never a
        # generic raw-argv path -- this is the one fixed argv this kind
        # ever produces.
        return ["worktree", "list", "--porcelain", "-z"]
    if operation.kind == "delete_private_ref_cas":
        private_ref = _revalidate_private_ref(operation.private_ref)
        object_id = _revalidate_object_id(operation.object_id)
        return ["update-ref", "-d", private_ref.value, object_id.value]
    raise AssertionError("unreachable_supported_operation")


def _exact_git_argv(
    operation: _GitOperation, *, git_executable: str, cwd: str, hooks_dir: str
) -> list[str]:
    no_lazy_fetch = (
        ("--no-lazy-fetch",)
        if operation.kind
        in {
            "probe_no_lazy_fetch_support",
            "fetch_default_ref_no_lazy",
        }
        else ()
    )
    # Issue #2199 AC1/In Scope: avoid optional-lock (index) contention on
    # this read-only listing operation, without adding `--no-optional-locks`
    # to any write/fetch/mutating operation kind.
    no_optional_locks = (
        ("--no-optional-locks",)
        if operation.kind == "list_worktrees_porcelain_locked_prunable"
        else ()
    )
    return [
        git_executable,
        "--no-replace-objects",
        *no_optional_locks,
        "-c",
        "core.hooksPath=" + hooks_dir,
        "-c",
        "credential.helper=",
        *no_lazy_fetch,
        *_operation_arguments(operation, cwd=cwd),
    ]


_INVOCATION_SUPERVISOR_MAX_RESULT_BYTES = 32 * 1024 * 1024
_INVOCATION_SUPERVISOR_READY = b"R"
_INVOCATION_SUPERVISOR_CLEANUP_ABSENT = b"A"
_INVOCATION_SUPERVISOR_CLEANUP_UNCONFIRMED = b"U"


class _InvocationSupervisorCleanupRequested(BaseException):
    """Ask the isolated supervisor to clean its Git-only child tree."""


def _request_invocation_supervisor_cleanup(_signum: int, _frame: object) -> None:
    raise _InvocationSupervisorCleanupRequested()


def _record_git_descendant_observation(
    proc: subprocess.Popen, descendants: set[_TrackedGitDescendant] | None
) -> set[_TrackedGitDescendant] | None:
    """Merge an advisory observation without letting its failure skip cleanup."""
    try:
        observed = _observe_git_descendants(proc.pid)
    except BaseException:  # noqa: BLE001
        return None
    if descendants is None or observed is None:
        return None
    descendants.update(observed)
    return descendants


def _drain_invocation_children_after_leader_exit(pgid: int, deadline: GitProtocolDeadline) -> str:
    """Classify only supervisor-adopted Git children after leader exit.

    A helper in the original dedicated Git session is an ordinary Git
    descendant completing shutdown, even when a non-interactive shell gives it
    another process group. An adopted child in another session escaped that
    containment and remains a fail-closed leak. Both cases are observed solely
    inside the short-lived invocation supervisor.
    """
    # start_new_session=True makes the leader PID both session and process
    # group ID; retain that numeric session identity after the leader is reaped.
    leader_sid = pgid
    drain_until = min(
        deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2)
    )
    while time.monotonic() < drain_until:
        children = _observe_invocation_supervisor_children()
        if children is None:
            return "unconfirmed"
        if not children:
            return "absent"
        for child in children:
            identity = _linux_process_identity(child.pid)
            if identity is None or identity[2] != child.start_time:
                continue
            try:
                if os.getsid(child.pid) != leader_sid:
                    if not _terminate_invocation_supervisor_children(deadline):
                        return "unconfirmed"
                    # It is still a Git-derived child in this isolated
                    # supervisor. Confirmed cleanup is sufficient; do not
                    # reclassify it as a host child or leave it running.
                    return "absent"
            except OSError:
                return "unconfirmed"
        if not _reap_tracked_children(children):
            return "unconfirmed"
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    children = _observe_invocation_supervisor_children()
    if children is None:
        return "unconfirmed"
    if not children:
        return "absent"
    if not _terminate_invocation_supervisor_children(deadline):
        return "unconfirmed"
    return "absent"


def _run_closed_git_process_in_invocation_supervisor(
    operation: _GitOperation,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> subprocess.CompletedProcess:
    """Run one Git command inside its short-lived subreaper supervisor."""
    if not _enable_linux_child_subreaper():
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_containment_unavailable")
    argv = _exact_git_argv(operation, git_executable=git_executable, cwd=cwd, hooks_dir=hooks_dir)
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": env,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": operation.kind != "probe_rewrite",
    }
    posix_supervised = _POSIX_PROCESS_GROUP_SUPPORTED
    if posix_supervised:
        kwargs["start_new_session"] = True
    proc: subprocess.Popen | None = None
    pgid: int | None = None
    descendants: set[_TrackedGitDescendant] | None = set() if posix_supervised else None
    try:
        proc = subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]
        pgid = proc.pid if posix_supervised else None
        # This initial observation is deliberately inside the guarded region:
        # observer failures occur after Popen and therefore require bounded
        # cleanup just like communicate(), poll(), and timeout failures.
        if posix_supervised:
            descendants = _record_git_descendant_observation(proc, descendants)
        while True:
            if posix_supervised:
                descendants = _record_git_descendant_observation(proc, descendants)
            try:
                stdout, stderr = proc.communicate(
                    timeout=min(_GROUP_POLL_INTERVAL_SECONDS, deadline.execution_seconds())
                )
                break
            except subprocess.TimeoutExpired:
                continue
    except GitProtocolDeadlineExhausted as exc:
        if proc is None or not _terminate_git_process_group(proc, pgid, deadline, descendants):
            raise GitProtocolProcessGroupCleanupFailed(
                "git_process_group_cleanup_unconfirmed"
            ) from exc
        raise GitProtocolTimeout("git_process_timeout") from exc
    except BaseException as exc:
        if proc is None or not _terminate_git_process_group(proc, pgid, deadline, descendants):
            raise GitProtocolProcessGroupCleanupFailed(
                "git_process_group_cleanup_unconfirmed"
            ) from exc
        raise

    # A non-success result is terminal, except the documented no-match result
    # of the rewrite probe. Cleanup remains mandatory before its result leaves
    # this supervisor.
    if proc.returncode != 0 and not (operation.kind == "probe_rewrite" and proc.returncode == 1):  # noqa: SIM102
        if not _terminate_git_process_group(proc, pgid, deadline, descendants):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    if posix_supervised and descendants is None:
        _terminate_git_process_group(proc, pgid, deadline, descendants)
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")

    # communicate() has reaped the direct Git leader. The invocation
    # supervisor may still have adopted a normal helper in that leader's
    # process group; drain it without widening attribution to any host child.
    if not posix_supervised or pgid is None:
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    drain_result = _drain_invocation_children_after_leader_exit(pgid, deadline)
    if drain_result == "unconfirmed":
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    if drain_result != "absent":
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_descendant_leak")
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _encode_invocation_supervisor_stream(value: str | bytes | None) -> dict[str, object]:
    if isinstance(value, str):
        raw = value.encode("utf-8")
        text = True
    elif isinstance(value, bytes):
        raw = value
        text = False
    else:
        raw = b""
        text = True
    return {"text": text, "data": base64.b64encode(raw).decode("ascii")}


def _decode_invocation_supervisor_stream(value: object) -> str | bytes | None:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("text"), bool)
        or not isinstance(value.get("data"), str)
    ):
        raise ValueError("invalid_invocation_supervisor_stream")  # noqa: TRY004
    raw = base64.b64decode(value["data"], validate=True)
    return raw.decode("utf-8") if value["text"] else raw


def _write_invocation_supervisor_result(write_fd: int, outcome: dict[str, object]) -> None:
    """Send one bounded JSON frame over a private parent/supervisor pipe."""
    try:
        payload = json.dumps(outcome, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > _INVOCATION_SUPERVISOR_MAX_RESULT_BYTES:
            return
        frame = struct.pack("!Q", len(payload)) + payload
        sent = 0
        while sent < len(frame):
            sent += os.write(write_fd, frame[sent:])
    except (OSError, TypeError, ValueError):
        return


def _supervisor_cleanup_deadline(deadline: GitProtocolDeadline) -> GitProtocolDeadline:
    """Reserve a bounded post-interruption window for the Git-only handshake."""
    return GitProtocolDeadline(
        deadline_at=max(
            deadline.deadline_at,
            time.monotonic() + max(0.1, deadline.cleanup_reserve_seconds),
        ),
        cleanup_reserve_seconds=deadline.cleanup_reserve_seconds,
    )


def _write_invocation_supervisor_cleanup_status(write_fd: int, status: bytes) -> None:
    try:
        os.write(write_fd, status)
    except OSError:
        pass


def _invocation_supervisor_main(
    write_fd: int,
    ready_write_fd: int,
    cleanup_write_fd: int,
    operation: _GitOperation,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> None:
    """Execute one Git operation and certify Git-only cleanup before exit."""
    handler_installed = False
    try:
        # The parent waits for this byte before it can interrupt result reading.
        # Therefore a parent-requested SIGTERM cannot arrive before this handler
        # converts it into the normal, bounded Git cleanup path.
        signal.signal(signal.SIGTERM, _request_invocation_supervisor_cleanup)
        handler_installed = True
        _write_invocation_supervisor_cleanup_status(ready_write_fd, _INVOCATION_SUPERVISOR_READY)
        try:
            completed = _run_closed_git_process_in_invocation_supervisor(
                operation,
                git_executable=git_executable,
                cwd=cwd,
                env=env,
                hooks_dir=hooks_dir,
                deadline=deadline,
            )
            outcome: dict[str, object] = {
                "kind": "completed",
                "argv": completed.args,
                "returncode": completed.returncode,
                "stdout": _encode_invocation_supervisor_stream(completed.stdout),
                "stderr": _encode_invocation_supervisor_stream(completed.stderr),
            }
        except BaseException as exc:  # noqa: BLE001
            outcome = {"kind": "raised", "type": type(exc).__name__, "message": str(exc)}
        _write_invocation_supervisor_result(write_fd, outcome)
    except BaseException as exc:  # noqa: BLE001
        # A signal during result serialization still reaches the final cleanup
        # handshake. Its result is deliberately not trusted by the parent.
        _write_invocation_supervisor_result(
            write_fd, {"kind": "raised", "type": type(exc).__name__, "message": str(exc)}
        )
    finally:
        if handler_installed:
            try:
                # Once cancellation has been accepted, do not permit a second
                # SIGTERM to interrupt the cleanup proof.
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
        cleanup_confirmed = _terminate_invocation_supervisor_children(
            _supervisor_cleanup_deadline(deadline)
        )
        _write_invocation_supervisor_cleanup_status(
            cleanup_write_fd,
            _INVOCATION_SUPERVISOR_CLEANUP_ABSENT
            if cleanup_confirmed
            else _INVOCATION_SUPERVISOR_CLEANUP_UNCONFIRMED,
        )
        for fd in (write_fd, ready_write_fd, cleanup_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        os._exit(0 if cleanup_confirmed else 1)


def _read_invocation_supervisor_cleanup_status(
    read_fd: int, deadline_at: float, *, expected: bytes
) -> bool:
    """Read one bounded supervisor-only handshake byte and close its pipe."""
    try:
        while time.monotonic() < deadline_at:
            readable, _writable, _exceptional = select.select(
                [read_fd], [], [], min(_GROUP_POLL_INTERVAL_SECONDS, deadline_at - time.monotonic())
            )
            if not readable:
                continue
            return os.read(read_fd, 1) == expected
        return False
    except (OSError, ValueError):
        return False
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


def _wait_for_invocation_supervisor_reap(supervisor_pid: int, deadline_at: float) -> bool:
    """Reap only the known direct supervisor, never a host process."""
    while time.monotonic() < deadline_at:
        try:
            waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
        except ChildProcessError:
            # SIGCHLD may be configured for automatic reaping. The cleanup
            # pipe is still bound to the original supervisor, so it remains a
            # valid absence certificate without any PID-directed action.
            return True
        except OSError:
            return False
        if waited_pid == supervisor_pid:
            return True
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    return False


def _stop_invocation_supervisor(supervisor_pid: int) -> None:
    """Last-resort SIGKILL of a still-known direct supervisor only."""
    try:
        waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return
    if waited_pid == supervisor_pid:
        return
    try:
        os.kill(supervisor_pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    try:
        os.waitpid(supervisor_pid, 0)
    except ChildProcessError:
        pass


def _cleanup_interrupted_invocation_supervisor(
    supervisor_pid: int, cleanup_read_fd: int, deadline: GitProtocolDeadline
) -> bool:
    """Request and verify bounded Git-only cleanup before propagating failure.

    The parent can signal and reap exactly its known one-invocation supervisor.
    It never scans, signals, or reaps a host child. The supervisor's subreaper
    acknowledgement is accepted only after its direct-child set proves the Git
    leader and every escaped descendant absent.
    """
    # The supervisor may first spend its ordinary reserved cleanup phase
    # terminating the leader and then run the final subreaper absence proof.
    # Keep the parent handshake bounded while allowing both Git-only phases.
    cleanup_deadline_at = max(
        deadline.deadline_at,
        time.monotonic() + max(0.1, deadline.cleanup_reserve_seconds) * 3,
    )
    try:
        waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        try:
            os.close(cleanup_read_fd)
        except OSError:
            pass
        return False
    supervisor_reaped = waited_pid == supervisor_pid
    if not supervisor_reaped:
        try:
            os.kill(supervisor_pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                os.close(cleanup_read_fd)
            except OSError:
                pass
            return False
    cleanup_absent = _read_invocation_supervisor_cleanup_status(
        cleanup_read_fd, cleanup_deadline_at, expected=_INVOCATION_SUPERVISOR_CLEANUP_ABSENT
    )
    if cleanup_absent and (
        supervisor_reaped
        or _wait_for_invocation_supervisor_reap(supervisor_pid, cleanup_deadline_at)
    ):
        return True
    _stop_invocation_supervisor(supervisor_pid)
    return False


def _read_invocation_supervisor_result(
    read_fd: int, supervisor_pid: int, deadline: GitProtocolDeadline
) -> dict[str, object] | None:
    """Read a bounded result while retaining a deadline for supervisor cleanup."""
    data = bytearray()
    result_deadline_at = deadline.deadline_at + max(0.1, _GROUP_POLL_INTERVAL_SECONDS * 2)
    try:
        while time.monotonic() < result_deadline_at:
            readable, _writable, _exceptional = select.select(
                [read_fd],
                [],
                [],
                min(_GROUP_POLL_INTERVAL_SECONDS, result_deadline_at - time.monotonic()),
            )
            if not readable:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _INVOCATION_SUPERVISOR_MAX_RESULT_BYTES + 8:
                return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
    if len(data) < 8:
        return None
    size = struct.unpack("!Q", data[:8])[0]
    if size > _INVOCATION_SUPERVISOR_MAX_RESULT_BYTES or len(data) != size + 8:
        return None
    try:
        outcome = json.loads(data[8:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(outcome, dict) or outcome.get("kind") not in {"completed", "raised"}:
        return None
    while time.monotonic() < result_deadline_at:
        try:
            waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
        except OSError:
            return None
        if waited_pid == supervisor_pid:
            return outcome
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    return None


def _raise_invocation_supervisor_error(outcome: dict[str, object]) -> None:
    error_type = outcome.get("type")
    message = outcome.get("message")
    if not isinstance(error_type, str) or not isinstance(message, str):
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    error_classes: dict[str, type[BaseException]] = {
        "GitProtocolDeadlineExhausted": GitProtocolDeadlineExhausted,
        "GitProtocolTimeout": GitProtocolTimeout,
        "GitProtocolProcessGroupCleanupFailed": GitProtocolProcessGroupCleanupFailed,
        "RuntimeError": RuntimeError,
        "ValueError": ValueError,
        "TypeError": TypeError,
    }
    error_class = error_classes.get(error_type)
    if error_class is None:
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    raise error_class(message)


def _run_closed_git_process(
    operation: _GitOperation,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> subprocess.CompletedProcess:
    """Delegate one Git process to a one-invocation subreaper supervisor.

    The host creates and waits for only the known supervisor PID. It never
    enables subreaping, scans `/proc` for host children, signals them, or reaps
    them. A failed or interrupted result read first performs a bounded,
    supervisor-certified Git-only cleanup handshake before it propagates.
    """
    deadline.execution_seconds()
    try:
        result_read_fd, result_write_fd = os.pipe()
        ready_read_fd, ready_write_fd = os.pipe()
        cleanup_read_fd, cleanup_write_fd = os.pipe()
        supervisor_pid = os.fork()
    except (AttributeError, OSError) as exc:
        raise GitProtocolProcessGroupCleanupFailed(
            "git_process_group_containment_unavailable"
        ) from exc
    if supervisor_pid == 0:
        try:
            for fd in (result_read_fd, ready_read_fd, cleanup_read_fd):
                os.close(fd)
            _invocation_supervisor_main(
                result_write_fd,
                ready_write_fd,
                cleanup_write_fd,
                operation,
                git_executable=git_executable,
                cwd=cwd,
                env=env,
                hooks_dir=hooks_dir,
                deadline=deadline,
            )
        finally:
            os._exit(1)
    for fd in (result_write_fd, ready_write_fd, cleanup_write_fd):
        os.close(fd)

    startup_deadline_at = min(
        deadline.deadline_at,
        time.monotonic() + max(0.1, deadline.cleanup_reserve_seconds),
    )
    try:
        supervisor_ready = _read_invocation_supervisor_cleanup_status(
            ready_read_fd, startup_deadline_at, expected=_INVOCATION_SUPERVISOR_READY
        )
    except BaseException as exc:
        try:
            os.close(result_read_fd)
        except OSError:
            pass
        if not _cleanup_interrupted_invocation_supervisor(
            supervisor_pid, cleanup_read_fd, deadline
        ):
            raise GitProtocolProcessGroupCleanupFailed(
                "git_process_group_cleanup_unconfirmed"
            ) from exc
        raise
    if not supervisor_ready:
        try:
            os.close(result_read_fd)
        except OSError:
            pass
        if not _cleanup_interrupted_invocation_supervisor(
            supervisor_pid, cleanup_read_fd, deadline
        ):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")

    try:
        outcome = _read_invocation_supervisor_result(result_read_fd, supervisor_pid, deadline)
    except BaseException as exc:
        try:
            # The normal reader closes this descriptor in its finally block;
            # repeat it defensively so an interrupted/custom reader cannot
            # block the supervisor while it reports cancellation.
            os.close(result_read_fd)
        except OSError:
            pass
        if not _cleanup_interrupted_invocation_supervisor(
            supervisor_pid, cleanup_read_fd, deadline
        ):
            raise GitProtocolProcessGroupCleanupFailed(
                "git_process_group_cleanup_unconfirmed"
            ) from exc
        raise
    if outcome is None:
        if not _cleanup_interrupted_invocation_supervisor(
            supervisor_pid, cleanup_read_fd, deadline
        ):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")

    # Result success is not a cleanup proof. The isolated supervisor writes
    # this acknowledgement only after its subreaper observes no Git leader or
    # escaped descendants and exits; `_read_invocation_supervisor_result` has
    # already reaped that exact known supervisor PID.
    normal_cleanup_deadline_at = _supervisor_cleanup_deadline(deadline).deadline_at
    if not _read_invocation_supervisor_cleanup_status(
        cleanup_read_fd, normal_cleanup_deadline_at, expected=_INVOCATION_SUPERVISOR_CLEANUP_ABSENT
    ):
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    if outcome["kind"] == "completed":
        try:
            argv = outcome["argv"]
            returncode = outcome["returncode"]
            if (
                not isinstance(argv, list)
                or not all(isinstance(arg, str) for arg in argv)
                or not isinstance(returncode, int)
            ):
                raise ValueError("invalid_invocation_supervisor_result")
            return subprocess.CompletedProcess(
                argv,
                returncode,
                _decode_invocation_supervisor_stream(outcome.get("stdout")),
                _decode_invocation_supervisor_stream(outcome.get("stderr")),
            )
        except (TypeError, ValueError, KeyError):
            raise GitProtocolProcessGroupCleanupFailed(
                "git_process_group_cleanup_unconfirmed"
            ) from None
    _raise_invocation_supervisor_error(outcome)
    raise AssertionError("unreachable_invocation_supervisor_error")


def _git_supports_no_lazy_fetch(
    git_executable: str,
    *,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> bool:
    """Probe the trusted executable once; never infer capability from an env var."""
    cached = _GIT_NO_LAZY_FETCH_CAPABILITY_CACHE.get(git_executable)
    if cached is not None:
        return cached
    probe = _run_closed_git_process(
        _GitOperation("probe_no_lazy_fetch_support"),
        git_executable=git_executable,
        cwd=cwd,
        env=env,
        hooks_dir=hooks_dir,
        deadline=deadline,
    )
    supported = probe.returncode == 0
    _GIT_NO_LAZY_FETCH_CAPABILITY_CACHE[git_executable] = supported
    return supported


def _repository_has_promisor_remote(
    git_executable: str,
    *,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> bool:
    """Detect a local partial-clone promisor without consulting ambient config."""
    probe = _run_closed_git_process(
        _GitOperation("probe_promisor_remote"),
        git_executable=git_executable,
        cwd=cwd,
        env=env,
        hooks_dir=hooks_dir,
        deadline=deadline,
    )
    if probe.returncode == 1:
        return False
    if probe.returncode != 0:
        raise RuntimeError(f"promisor_remote_probe_failed:{probe.returncode}")
    return any(
        line.rsplit(maxsplit=1)[-1].lower() in {"true", "yes", "on", "1"}
        for line in (probe.stdout or "").splitlines()
        if line.split()
    )


def _require_literal_effective_remote_url(
    remote_url: LiteralRemoteUrl,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> None:
    """Fail closed only when Git rewrites the transport actually in use.

    An unrelated `insteadOf` or any `pushInsteadOf` key is not authority for a
    read/fetch protocol. `ls-remote --get-url` is local/no-network and returns
    the exact URL Git would use for this literal remote.
    """
    effective = _require_success(
        _run_closed_git_process(
            _GitOperation("effective_remote_url", remote_url=remote_url),
            git_executable=git_executable,
            cwd=cwd,
            env=env,
            hooks_dir=hooks_dir,
            deadline=deadline,
        ),
        "effective_remote_url",
    )
    if (effective.stdout or "").rstrip("\n") != remote_url.value:
        raise RuntimeError("effective_remote_url_mismatch")


def _execute_semantic_git(
    operation: _GitOperation,
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None,
    deadline: GitProtocolDeadline,
) -> subprocess.CompletedProcess:
    operation = _revalidate_semantic_operation(operation, project_root)
    deadline.execution_seconds()
    git = resolve_git_subprocess_executable(project_root)
    env = sanitized_git_subprocess_env(project_root)
    hooks_dir = git_subprocess_trusted_hooks_dir(scratch_root or project_root)
    try:
        if operation.kind in {"observe_default_ref", "fetch_default_ref"}:
            remote_url = _revalidate_literal_remote_url(operation.remote_url)
            _require_literal_effective_remote_url(
                remote_url,
                git_executable=git,
                cwd=cwd,
                env=env,
                hooks_dir=hooks_dir,
                deadline=deadline,
            )
        if operation.kind == "fetch_default_ref":
            if _git_supports_no_lazy_fetch(
                git, cwd=cwd, env=env, hooks_dir=hooks_dir, deadline=deadline
            ):
                operation = _GitOperation(
                    "fetch_default_ref_no_lazy",
                    remote_url=operation.remote_url,
                    remote_ref=operation.remote_ref,
                    private_ref=operation.private_ref,
                )
            elif _repository_has_promisor_remote(
                git, cwd=cwd, env=env, hooks_dir=hooks_dir, deadline=deadline
            ):
                raise RuntimeError("git_no_lazy_fetch_not_supported_for_promisor_repository")
        return _run_closed_git_process(
            operation,
            git_executable=git,
            cwd=cwd,
            env=env,
            hooks_dir=hooks_dir,
            deadline=deadline,
        )
    finally:
        try:
            shutil.rmtree(hooks_dir)
        except OSError as exc:
            raise GitProtocolProcessGroupCleanupFailed(
                "git_subprocess_hooks_cleanup_failed"
            ) from exc


def _require_success(
    result: subprocess.CompletedProcess, operation: str
) -> subprocess.CompletedProcess:
    if result.returncode != 0:
        raise RuntimeError(
            f"{operation}_failed:{result.returncode}:{(result.stderr or '').strip()}"
        )
    return result


def parse_control_plane_default_ref_symref(stdout: str) -> tuple[str, str]:
    """Focused, single-purpose parser for the exact output shape of
    ``git ls-remote --exit-code --symref <canonical-literal> HEAD`` (Issue
    #2197 AC2). Not a generic ls-remote/symref parsing framework: it only
    ever accepts a request for exactly one ref (``HEAD``) and rejects
    anything but that request's own well-formed two-line symref shape.

    Each non-empty output line must be ``<left>\\t<right>``. ``right`` must be
    exactly ``HEAD`` -- a request for any other ref name never reaches this
    parser, so anything else here is malformed. ``left`` is classified into
    either a symbolic-target line (``ref: <target>``) or an object-id
    candidate line (anything else). Exactly one symbolic-target line and
    exactly one object-id line are required; any other count -- zero
    symbolic-target lines (unborn or non-symbolic/direct ``HEAD``), more than
    one of either kind (duplicate/ambiguous), or a missing/extra object-id
    line -- fails closed with ``ValueError``.

    This function only proves *shape*. It deliberately does not itself
    validate the symbolic target against the ``refs/heads/`` namespace grammar
    or the object id against a specific hash length: those are the existing,
    already-reviewed responsibilities of ``validate_allowed_remote_ref`` and
    ``validate_repository_object_id``, which the caller applies to this
    function's return value. A ref target outside ``refs/heads/`` or an
    abbreviated/invalid-length/invalid-character object id therefore still
    fails closed, just one layer up, without this parser duplicating that
    grammar.
    """
    ref_targets: list[str] = []
    object_id_candidates: list[str] = []
    for raw_line in stdout.split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if "\t" not in line:
            raise ValueError("control_plane_symref_line_malformed")
        left, right = line.split("\t", 1)
        if right != "HEAD" or not left:
            raise ValueError("control_plane_symref_line_malformed")
        if left.startswith("ref: "):
            ref_targets.append(left[len("ref: ") :])
        else:
            object_id_candidates.append(left)
    if len(ref_targets) != 1:
        raise ValueError("control_plane_symref_ref_line_count_invalid")
    if len(object_id_candidates) != 1:
        raise ValueError("control_plane_symref_object_id_line_count_invalid")
    return ref_targets[0], object_id_candidates[0]


def run_control_plane_git_effective_remote_url(
    expected_remote_url: str,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> LiteralRemoteUrl:
    literal = validate_literal_remote_url(expected_remote_url)
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("effective_remote_url", remote_url=literal),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "effective_remote_url",
    )
    if (result.stdout or "").rstrip("\n") != literal.value:
        raise RuntimeError("effective_remote_url_mismatch")
    return literal


def run_control_plane_git_observe_default_ref(
    remote_url: LiteralRemoteUrl,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> subprocess.CompletedProcess:
    remote_url = _revalidate_literal_remote_url(remote_url)
    return _require_success(
        _execute_semantic_git(
            _GitOperation("observe_default_ref", remote_url=remote_url),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "observe_default_ref",
    )


def run_control_plane_git_repository_object_format(
    *, cwd: str, project_root: str, deadline: GitProtocolDeadline, scratch_root: str | None = None
) -> RepositoryObjectFormat:
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("repository_object_format"),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "repository_object_format",
    )
    return validate_repository_object_format((result.stdout or "").strip())


def run_control_plane_git_fetch_default_ref(
    remote_url: LiteralRemoteUrl,
    remote_ref: AllowedRemoteRef,
    *,
    object_format: RepositoryObjectFormat,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> ControlPlanePrivateRef:
    """Fetch exactly ``remote_ref`` from ``remote_url`` into a fresh nonce
    private ref this builder allocates itself (Issue #2197 AC3/AC4).

    Once ``private_ref`` is generated, this function owns that ref's own
    cleanup lifecycle end to end for every exception raised *inside* this one
    call -- including a genuine git-fetch failure and a post-command cleanup
    failure. The trusted hooks-dir removal in `_execute_semantic_git`'s own
    ``finally`` block can itself raise, and per Python's ``finally``
    semantics that overrides the ``try`` block's return even when the
    underlying fetch subprocess already succeeded and mutated the private
    ref -- so this function's own caller must never learn a private ref name
    it cannot also account for the cleanup of.

    On any such exception, a best-effort read-back of ``private_ref``'s
    current OID decides what, if anything, needs cleanup: an unreadable ref
    (the fetch never actually completed) means there is nothing to clean up.
    A readable ref attempts the same CAS delete already used elsewhere. The
    exception that reaches the caller is always `ControlPlaneUnavailable`,
    chained from the original failure when cleanup itself needed no further
    action or succeeded, or chained from the cleanup failure (reusing the
    same ``private_ref_cleanup_failed`` reason string convention already
    used by `run_control_plane_remote_default_ref_binding`) when cleanup
    itself also failed -- never silently swallowed.

    This complements, and does not duplicate,
    `run_control_plane_remote_default_ref_binding`'s own except-block CAS
    cleanup, which only ever runs for exceptions occurring *after* this call
    has already returned.
    """
    remote_url = _revalidate_literal_remote_url(remote_url)
    remote_ref = _revalidate_allowed_remote_ref(remote_ref)
    object_format = _revalidate_object_format(object_format)
    private_ref = make_control_plane_private_ref()
    try:
        _require_success(
            _execute_semantic_git(
                _GitOperation(
                    "fetch_default_ref",
                    remote_url=remote_url,
                    remote_ref=remote_ref,
                    private_ref=private_ref,
                ),
                cwd=cwd,
                project_root=project_root,
                scratch_root=scratch_root,
                deadline=deadline,
            ),
            "fetch_default_ref",
        )
    except BaseException as exc:
        cleanup_deadline = _cleanup_deadline(deadline)
        try:
            fetched_oid = run_control_plane_git_read_private_ref_oid(
                private_ref,
                object_format,
                cwd=cwd,
                project_root=project_root,
                deadline=cleanup_deadline,
                scratch_root=scratch_root,
            )
        except BaseException:  # noqa: BLE001
            raise ControlPlaneUnavailable(f"control_plane_unavailable:fetch_failed:{exc}") from exc
        try:
            run_control_plane_git_delete_private_ref_cas(
                private_ref,
                fetched_oid,
                cwd=cwd,
                project_root=project_root,
                deadline=cleanup_deadline,
                scratch_root=scratch_root,
            )
        except BaseException as cleanup_exc:
            raise ControlPlaneUnavailable(
                "control_plane_unavailable:private_ref_cleanup_failed"
            ) from cleanup_exc
        raise ControlPlaneUnavailable(f"control_plane_unavailable:fetch_failed:{exc}") from exc
    return private_ref


def run_control_plane_git_read_private_ref_oid(
    private_ref: ControlPlanePrivateRef,
    object_format: RepositoryObjectFormat,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> RepositoryObjectId:
    private_ref = _revalidate_private_ref(private_ref)
    object_format = _revalidate_object_format(object_format)
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("read_private_ref_oid", private_ref=private_ref),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "read_private_ref_oid",
    )
    return validate_repository_object_id((result.stdout or "").strip(), object_format)


def run_control_plane_git_require_commit_object(
    object_id: RepositoryObjectId,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> None:
    object_id = _revalidate_object_id(object_id)
    _require_success(
        _execute_semantic_git(
            _GitOperation("require_commit_object", object_id=object_id),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "commit_object",
    )


def run_control_plane_git_read_worktree_head(
    path: DetachedWorktreePath,
    object_format: RepositoryObjectFormat,
    *,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> RepositoryObjectId:
    path = _revalidate_worktree_path(path, project_root, require_fresh=False)
    object_format = _revalidate_object_format(object_format)
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("read_worktree_head", worktree_path=path),
            cwd=path.value,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "read_worktree_head",
    )
    return validate_repository_object_id((result.stdout or "").strip(), object_format)


def run_control_plane_git_read_worktree_status_porcelain(
    path: DetachedWorktreePath,
    *,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> str:
    """Read-only ``git status --porcelain --untracked-files=all`` for an
    existing fixed-identity worktree (Issue #2197 AC5 cleanliness check).
    Empty string means clean. This performs no mutation and no identity
    verification of its own -- the caller decides what "clean" permits."""
    path = _revalidate_worktree_path(path, project_root, require_fresh=False)
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("read_worktree_status_porcelain", worktree_path=path),
            cwd=path.value,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "read_worktree_status_porcelain",
    )
    return result.stdout or ""


def run_control_plane_git_list_worktrees_porcelain_locked_prunable(
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> str:
    """Read-only ``git worktree list --porcelain -z`` exposing ``locked``/
    ``prunable`` (Issue #2199 AC2/AC10/AC12).

    This is the minimal typed extension this Issue adds to the existing
    closed/sanitized Git execution seam (#2378/#2197) so the dedicated-lane
    identity probe (``worktree_bootstrap_exec.verify_dedicated_control_plane_identity``,
    consumed via ``worktree_catalog.parse_worktree_porcelain_locked_prunable_z``)
    never needs a generic raw-argv Git executor or an ambient
    ``subprocess.run(["git", ...])`` call of its own. It performs no mutation
    and no identity verification itself -- the caller interprets the raw
    porcelain text.
    """
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("list_worktrees_porcelain_locked_prunable"),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "list_worktrees_porcelain_locked_prunable",
    )
    return result.stdout or ""


def run_control_plane_git_remove_existing_detached_locked_worktree(
    path: DetachedWorktreePath,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> None:
    """Remove exactly the caller-supplied existing detached+locked worktree
    path (Issue #2197 AC5 controlled refresh/recreate transition).

    This is a narrow, single-purpose wrapper around the already-closed
    ``remove_detached_locked_worktree`` operation kind -- the exact same
    operation kind and argv shape `_rollback_detached_locked_worktree`
    already uses internally for its own post-add rollback. It is not a new
    generic force-remove worktree API (Issue #2197 Out of Scope): there is no
    caller-selectable flag/mode, and the only accepted argument is a path
    that has already passed `validate_existing_detached_worktree_path`'s
    fixed `.claude/worktrees` confinement. Callers are responsible for
    verifying fixed-worktree identity, git-common-dir linkage, and
    cleanliness *before* calling this -- it performs none of those checks
    itself.
    """
    path = _revalidate_worktree_path(path, project_root, require_fresh=False)
    _require_success(
        _execute_semantic_git(
            _GitOperation("remove_detached_locked_worktree", worktree_path=path),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "remove_detached_locked_worktree",
    )


def run_control_plane_remote_default_ref_binding(
    remote_url: LiteralRemoteUrl,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> tuple[ControlPlanePrivateRef, RepositoryObjectId, RepositoryObjectFormat]:
    """Closed S1 -> fetch -> F -> (optional one) S2 remote default-ref
    binding protocol (Issue #2197 AC1-AC4).

    S1 observes ``(ref1, oid1)`` from the canonical remote's ``HEAD`` symref
    via the focused parser (AC2). Exactly one fetch of ``ref1`` follows, into
    a fresh nonce private ref allocated by
    ``run_control_plane_git_fetch_default_ref`` -- this protocol never
    refetches. ``F`` is that private ref's local readback. ``F == oid1``
    accepts immediately. Otherwise exactly one further S2 observe is
    permitted, and is accepted only when ``S2.ref == ref1 and S2.oid == F``;
    any other outcome -- including a changed symbolic target -- is
    ``ControlPlaneUnavailable`` (AC3). Every ``observe_default_ref`` /
    ``fetch_default_ref`` call already re-verifies the resolved/literal
    remote identity inside ``_execute_semantic_git`` (AC1); this function
    does not duplicate that check.

    Once the fetch has succeeded, the private ref is a live resource this
    function owns end to end. Every exception raised past that point --
    including this protocol's own S2 rejection -- triggers a builder-owned
    best-effort CAS delete of that exact private ref (by the most recently
    confirmed expected OID) before the exception propagates. A cleanup
    failure itself is reported as ``ControlPlaneUnavailable``, chained from
    the underlying cleanup error, never silently swallowed (AC4/AC7). A
    failure *before* the fetch call (bad remote identity, S1 parse/shape
    rejection, object-format lookup failure) never creates a private ref, so
    it propagates unchanged -- there is nothing yet to clean up.
    """
    remote_url = _revalidate_literal_remote_url(remote_url)
    object_format = run_control_plane_git_repository_object_format(
        cwd=cwd, project_root=project_root, deadline=deadline, scratch_root=scratch_root
    )

    def _observe() -> tuple[AllowedRemoteRef, RepositoryObjectId]:
        observed = run_control_plane_git_observe_default_ref(
            remote_url,
            cwd=cwd,
            project_root=project_root,
            deadline=deadline,
            scratch_root=scratch_root,
        )
        raw_ref, raw_object_id = parse_control_plane_default_ref_symref(observed.stdout or "")
        return (
            validate_allowed_remote_ref(raw_ref),
            validate_repository_object_id(raw_object_id, object_format),
        )

    ref1, oid1 = _observe()
    private_ref = run_control_plane_git_fetch_default_ref(
        remote_url,
        ref1,
        object_format=object_format,
        cwd=cwd,
        project_root=project_root,
        deadline=deadline,
        scratch_root=scratch_root,
    )
    cleanup_expected_oid: RepositoryObjectId = oid1
    try:
        fetched_oid = run_control_plane_git_read_private_ref_oid(
            private_ref,
            object_format,
            cwd=cwd,
            project_root=project_root,
            deadline=deadline,
            scratch_root=scratch_root,
        )
        cleanup_expected_oid = fetched_oid
        if fetched_oid != oid1:
            ref2, oid2 = _observe()
            if ref2 != ref1 or oid2 != fetched_oid:
                raise ControlPlaneUnavailable(
                    "control_plane_unavailable:remote_default_ref_state_machine_rejected"
                )
        run_control_plane_git_require_commit_object(
            fetched_oid,
            cwd=cwd,
            project_root=project_root,
            deadline=deadline,
            scratch_root=scratch_root,
        )
        return private_ref, fetched_oid, object_format
    except BaseException:
        try:
            run_control_plane_git_delete_private_ref_cas(
                private_ref,
                cleanup_expected_oid,
                cwd=cwd,
                project_root=project_root,
                deadline=_cleanup_deadline(deadline),
                scratch_root=scratch_root,
            )
        except BaseException as cleanup_exc:
            raise ControlPlaneUnavailable(
                "control_plane_unavailable:private_ref_cleanup_failed"
            ) from cleanup_exc
        raise


def _cleanup_deadline(deadline: GitProtocolDeadline) -> GitProtocolDeadline:
    """Bound compensation deadline for cleanup-class operations (Issue #2197
    AC4/AC5 P1).

    `GitProtocolDeadline.execution_seconds()` always denies a normal
    operation once `remaining <= cleanup_reserve_seconds` -- that reserve
    exists so cleanup-class operations still have something to spend. But a
    cleanup-class operation given the *same* shared deadline is denied by
    that very reserve boundary too, defeating the reserve's own purpose.
    This helper gives cleanup-class operations (a detached-worktree-add
    rollback, or a builder-owned CAS delete of a fetched private ref) their
    own bounded lane that can actually spend (part of) the reserve that was
    set aside for them.

    This is not a new normal-protocol deadline, and it never unboundedly
    extends the original wall-clock deadline -- it is a small, bounded
    recovery window (a fixed multiple of the original
    `cleanup_reserve_seconds`) reserved solely for already-scoped cleanup
    purposes: removing a worktree this builder just created and proving both
    its path and its catalog entry are absent, or a best-effort CAS delete of
    a private ref this builder owns.
    """
    reserve = _validate_deadline_value(deadline.cleanup_reserve_seconds, "cleanup_reserve")
    return GitProtocolDeadline(
        deadline_at=max(deadline.deadline_at, time.monotonic() + reserve * 6),
        cleanup_reserve_seconds=reserve,
    )


def _worktree_catalog_contains_path(porcelain: str, path: DetachedWorktreePath) -> bool:
    target = os.path.realpath(path.value)
    return any(
        line.startswith("worktree ") and os.path.realpath(line.removeprefix("worktree ")) == target
        for line in porcelain.splitlines()
    )


def _rollback_detached_locked_worktree(
    path: DetachedWorktreePath,
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None,
    deadline: GitProtocolDeadline,
) -> None:
    recovery_deadline = _cleanup_deadline(deadline)
    if Path(path.value).exists():
        _require_success(
            _execute_semantic_git(
                _GitOperation("remove_detached_locked_worktree", worktree_path=path),
                cwd=cwd,
                project_root=project_root,
                scratch_root=scratch_root,
                deadline=recovery_deadline,
            ),
            "remove_detached_locked_worktree",
        )
    if Path(path.value).exists():
        raise RuntimeError("detached_worktree_rollback_path_present")
    catalog = _require_success(
        _execute_semantic_git(
            _GitOperation("list_worktrees_porcelain"),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=recovery_deadline,
        ),
        "list_worktrees_porcelain",
    )
    if _worktree_catalog_contains_path(catalog.stdout or "", path):
        raise RuntimeError("detached_worktree_rollback_catalog_present")


def run_control_plane_git_add_detached_locked_worktree(
    path: DetachedWorktreePath,
    commit: RepositoryObjectId,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> None:
    path = _revalidate_worktree_path(path, project_root, require_fresh=True)
    commit = _revalidate_object_id(commit)
    run_control_plane_git_require_commit_object(
        commit, cwd=cwd, project_root=project_root, deadline=deadline, scratch_root=scratch_root
    )
    add_attempted = False
    try:
        add_attempted = True
        _require_success(
            _execute_semantic_git(
                _GitOperation("add_detached_locked_worktree", object_id=commit, worktree_path=path),
                cwd=cwd,
                project_root=project_root,
                scratch_root=scratch_root,
                deadline=deadline,
            ),
            "add_detached_locked_worktree",
        )
        object_format = validate_repository_object_format(
            "sha1" if len(commit.value) == 40 else "sha256"
        )
        if (
            run_control_plane_git_read_worktree_head(
                path,
                object_format,
                project_root=project_root,
                deadline=deadline,
                scratch_root=scratch_root,
            )
            != commit
        ):
            raise RuntimeError("detached_worktree_head_mismatch")
    except BaseException:
        # A result-read interruption may happen after `git worktree add`
        # succeeded, so the fresh path is also checked, not only a local flag.
        if add_attempted and Path(path.value).exists():
            _rollback_detached_locked_worktree(
                path,
                cwd=cwd,
                project_root=project_root,
                scratch_root=scratch_root,
                deadline=deadline,
            )
        raise


def run_control_plane_git_delete_private_ref_cas(
    private_ref: ControlPlanePrivateRef,
    expected_oid: RepositoryObjectId,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> None:
    private_ref = _revalidate_private_ref(private_ref)
    expected_oid = _revalidate_object_id(expected_oid)
    _require_success(
        _execute_semantic_git(
            _GitOperation(
                "delete_private_ref_cas", private_ref=private_ref, object_id=expected_oid
            ),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "delete_private_ref_cas",
    )


if __name__ == "__main__":
    raise SystemExit(main())
