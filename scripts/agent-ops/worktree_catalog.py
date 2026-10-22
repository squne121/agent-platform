#!/usr/bin/env python3
"""worktree_catalog.py — shared git worktree catalog + monotonic deadline (Issue #1137).

Single source of truth for parsing ``git worktree list --porcelain -z`` so that
``worktree_scope_guard``, ``guard_preflight``, and ``cleanup_exec`` all resolve
worktrees identically (OWNER review Blocker 3 / Medium "porcelain -z 統一"). The
``-z`` form is used everywhere to avoid newline/quoting ambiguity in paths.

Also provides ``Deadline``, a shared monotonic budget so every guard subprocess
runs under one wall-clock ceiling smaller than the outer hook timeout (OWNER
review High "timeout"). The module is import-safe and has no side effects.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

SCHEMA_ENTRY = "WORKTREE_CATALOG_ENTRY_V1"

# Issue #2199 AC2/AC10/AC11/AC12: a SEPARATE, additive schema for the
# dedicated-lane identity probe only. `WORKTREE_CATALOG_ENTRY_V1` /
# `parse_worktree_porcelain_z()` above are never modified by this Issue --
# no `locked`/`prunable` field is added to that entry shape. This probe-only
# schema and its parser are consumed exclusively by dedicated worktree
# identity verification (`worktree_bootstrap_exec.py`), never by the
# existing `list_worktrees()` / `select_issue_worktree*()` catalog callers.
SCHEMA_IDENTITY_PROBE_ENTRY = "WORKTREE_IDENTITY_PROBE_ENTRY_V1"

# Reason code returned when the shared deadline is exhausted.
GUARD_DEADLINE_EXCEEDED = "guard_deadline_exceeded"


class GuardDeadlineExceeded(Exception):
    """Raised when a subprocess cannot run within the remaining shared budget."""


class Deadline:
    """A monotonic wall-clock budget shared across guard subprocesses.

    ``budget_seconds`` is the total time all checks may consume. ``remaining()``
    returns the seconds left (never negative). ``subprocess_timeout(maximum)``
    returns a per-call timeout clamped to the remaining budget so the sum of
    inner timeouts can never exceed the outer hook timeout.
    """

    def __init__(self, budget_seconds: float) -> None:
        self._budget = float(budget_seconds)
        self._start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining(self) -> float:
        return max(0.0, self._budget - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def subprocess_timeout(self, maximum: float) -> float:
        """Per-subprocess timeout: min(maximum, remaining). Raises if no budget."""
        rem = self.remaining()
        if rem <= 0.0:
            raise GuardDeadlineExceeded(GUARD_DEADLINE_EXCEEDED)
        return min(float(maximum), rem)


def parse_worktree_porcelain_z(data: str) -> list[dict]:
    """Parse ``git worktree list --porcelain -z`` (NUL-separated attribute lines).

    Returns a list of ``WORKTREE_CATALOG_ENTRY_V1`` dicts with keys
    ``worktree_realpath`` / ``branch_ref`` / ``git_common_dir`` / ``detached`` /
    ``exists_on_disk``.
    A record starts at a ``worktree <path>`` field and runs until the next one.
    """
    entries: list[dict] = []
    current: dict | None = None

    def _flush() -> None:
        if current is not None:
            entries.append(current)

    for field in data.split("\0"):
        if field == "":
            continue
        if field.startswith("worktree "):
            _flush()
            raw = field[len("worktree ") :]
            realpath = os.path.realpath(raw)
            current = {
                "schema": SCHEMA_ENTRY,
                "worktree_realpath": realpath,
                "branch_ref": None,
                "git_common_dir": None,
                "detached": False,
                "exists_on_disk": os.path.isdir(realpath),
            }
        elif current is None:
            continue
        elif field.startswith("branch "):
            ref = field[len("branch ") :]
            current["branch_ref"] = ref
        elif field == "detached":
            current["detached"] = True
        elif field.startswith("HEAD "):
            current["head"] = field[len("HEAD ") :]
    _flush()
    return entries


def parse_worktree_porcelain_locked_prunable_z(data: str) -> list[dict]:
    """Parse ``git worktree list --porcelain -z`` exposing ``locked``/``prunable``
    (Issue #2199 AC2/AC10/AC11/AC12).

    This is a deliberately SEPARATE parser from ``parse_worktree_porcelain_z``
    above -- it returns ``WORKTREE_IDENTITY_PROBE_ENTRY_V1`` dicts, a distinct
    shape that is never assigned to the existing ``WORKTREE_CATALOG_ENTRY_V1``
    schema and never consumed by ``list_worktrees()`` / ``select_issue_worktree*()``.
    Callers pass in porcelain text obtained via the existing closed/sanitized
    Git execution seam (``skill_runtime_exec.run_control_plane_git_list_worktrees_porcelain_locked_prunable``)
    -- this module itself performs no ambient ``subprocess.run(["git", ...])``
    for this probe.
    """
    entries: list[dict] = []
    current: dict | None = None

    def _flush() -> None:
        if current is not None:
            entries.append(current)

    for field in data.split("\0"):
        if field == "":
            continue
        if field.startswith("worktree "):
            _flush()
            raw = field[len("worktree ") :]
            realpath = os.path.realpath(raw)
            current = {
                "schema": SCHEMA_IDENTITY_PROBE_ENTRY,
                "worktree_realpath": realpath,
                "branch_ref": None,
                "detached": False,
                "head": None,
                "locked": False,
                "prunable": False,
                "exists_on_disk": os.path.isdir(realpath),
            }
        elif current is None:
            continue
        elif field.startswith("branch "):
            current["branch_ref"] = field[len("branch ") :]
        elif field == "detached":
            current["detached"] = True
        elif field.startswith("HEAD "):
            current["head"] = field[len("HEAD ") :]
        elif field == "locked" or field.startswith("locked "):
            current["locked"] = True
        elif field == "prunable" or field.startswith("prunable "):
            current["prunable"] = True
    _flush()
    return entries


def list_worktrees(project_root: str, deadline: Deadline | None = None) -> list[dict] | None:
    """Return the worktree catalog for ``project_root``, or None on git failure.

    Uses ``git worktree list --porcelain -z``. When a ``Deadline`` is supplied the
    subprocess timeout is clamped to the remaining budget.
    """
    git = shutil.which("git")
    if not git:
        return None
    timeout = deadline.subprocess_timeout(10.0) if deadline is not None else 10.0
    try:
        out = subprocess.run(  # noqa: PLW1510
            [git, "-C", project_root, "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    # git_common_dir is shared across linked worktrees; resolve once.
    common_dir = _git_common_dir(project_root, git, deadline)
    entries = parse_worktree_porcelain_z(out.stdout)
    for e in entries:
        e["git_common_dir"] = common_dir
    return entries


def _git_common_dir(project_root: str, git: str, deadline: Deadline | None) -> str | None:
    timeout = deadline.subprocess_timeout(5.0) if deadline is not None else 5.0
    try:
        out = subprocess.run(  # noqa: PLW1510
            [git, "-C", project_root, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    if not val:
        return None
    return os.path.realpath(os.path.join(project_root, val))


def find_by_realpath(catalog: list[dict], target_realpath: str) -> dict | None:
    """Return the catalog entry whose worktree_realpath equals target, else None."""
    target = os.path.realpath(target_realpath)
    for e in catalog:
        if e.get("worktree_realpath") == target:
            return e
    return None


def branch_short_name(branch_ref: str | None) -> str | None:
    """Strip refs/heads/ from a branch ref. Returns None for detached/None."""
    if not branch_ref:
        return None
    if branch_ref.startswith("refs/heads/"):
        return branch_ref[len("refs/heads/") :]
    return branch_ref


def select_issue_worktrees(
    catalog: list[dict], issue: str, root_realpath: str | None = None
) -> list[dict]:
    """Return catalog entries belonging to ``issue`` using a SINGLE shared rule.

    Issue #1137 Blocker 7: ``worktree_scope_guard`` and ``guard_preflight`` must
    select worktrees identically. The one canonical rule (fail-closed / strict)
    requires BOTH the branch short-name AND the path basename to match
    ``(worktree-)?issue-<issue>-*``. The root worktree is always excluded.
    """
    if not issue:
        return []
    branch_re = re.compile(rf"^(?:worktree-)?issue-{re.escape(issue)}-")
    base_re = re.compile(rf"^issue-{re.escape(issue)}-")
    root_real = os.path.realpath(root_realpath) if root_realpath else None
    out: list[dict] = []
    for e in catalog:
        wt = e.get("worktree_realpath")
        if not wt:
            continue
        if root_real is not None and wt == root_real:
            continue
        branch = branch_short_name(e.get("branch_ref"))
        base = os.path.basename(os.path.normpath(wt))
        branch_ok = bool(branch) and bool(branch_re.match(branch))
        base_ok = bool(base_re.match(base))
        if branch_ok and base_ok:
            out.append(e)
    return out


def select_issue_worktree(
    catalog: list[dict], issue: str, root_realpath: str | None = None
) -> dict | None:
    """Return the single unambiguous worktree for ``issue`` (None if 0 or >1)."""
    matches = select_issue_worktrees(catalog, issue, root_realpath)
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Canonical primary-root resolution (Issue #2979)
#
# ADDITIVE shared helpers. ``list_worktrees()`` above is intentionally left
# untouched (signature, ``None``-on-failure contract and the per-entry
# ``git_common_dir`` copy) because ``worktree_bootstrap_exec`` /
# ``git_worktree_probe`` / ``verified_*_merge_exec`` consume it unchanged.
#
# The helpers below decide "which checkout is the canonical repository root"
# from Git repository identity rather than from the session's current worktree:
#   * the FIRST ``git worktree list --porcelain -z`` entry is the primary
#     worktree;
#   * the candidate (linked worktree / primary / script location) and the
#     primary each run their OWN ``git rev-parse --git-common-dir`` and the two
#     realpaths are compared. The ``git_common_dir`` field that
#     ``list_worktrees()`` copies onto every entry is NEVER trusted -- a
#     comparison of that copied field would pass vacuously.
# ---------------------------------------------------------------------------

# Fixed reason-code literals (Issue #2979 Design Requirement 8). They are carried
# on EXISTING response fields only (guard: ``blocked_reason_codes``; executor:
# the existing refusal ``reason_code``); no new schema/field is introduced.
WORKTREE_CATALOG_UNAVAILABLE = "worktree_catalog_unavailable"
GIT_COMMON_DIR_UNAVAILABLE = "git_common_dir_unavailable"
PRIMARY_ROOT_UNRESOLVED = "primary_root_unresolved"
REPOSITORY_IDENTITY_MISMATCH = "repository_identity_mismatch"
ACTIVE_ISSUE_CATALOG_CONFLICT = "active_issue_catalog_conflict"

ROOT_RESOLUTION_REASON_CODES = (
    WORKTREE_CATALOG_UNAVAILABLE,
    GIT_COMMON_DIR_UNAVAILABLE,
    PRIMARY_ROOT_UNRESOLVED,
    REPOSITORY_IDENTITY_MISMATCH,
    ACTIVE_ISSUE_CATALOG_CONFLICT,
)


class RootResolution:
    """Outcome of :func:`resolve_canonical_root`.

    ``ok`` is True only when ``primary_root`` is a verified canonical primary
    work tree of the same repository as ``candidate``. ``catalog`` is the
    catalog the decision was based on (``None`` when it could not be obtained);
    callers must never replace a ``None`` catalog by ``[]``.
    """

    __slots__ = ("candidate", "catalog", "ok", "primary_root", "reason_code")

    def __init__(
        self,
        ok: bool,
        primary_root: str | None,
        reason_code: str | None,
        catalog: list[dict] | None,
        candidate: str,
    ) -> None:
        self.ok = ok
        self.primary_root = primary_root
        self.reason_code = reason_code
        self.catalog = catalog
        self.candidate = candidate

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"RootResolution(ok={self.ok!r}, primary_root={self.primary_root!r}, "
            f"reason_code={self.reason_code!r}, candidate={self.candidate!r})"
        )


def independent_git_common_dir(path: str, deadline: Deadline | None = None) -> str | None:
    """Run ``git rev-parse --git-common-dir`` INSIDE ``path`` and return its realpath.

    A relative answer (``.git``) is resolved against ``path`` -- the cwd the
    command ran in. Returns ``None`` when git is missing or the command fails.
    """
    git = shutil.which("git")
    if not git:
        return None
    return _git_common_dir(path, git, deadline)


def _is_inside_work_tree(path: str, git: str, deadline: Deadline | None) -> bool:
    timeout = deadline.subprocess_timeout(5.0) if deadline is not None else 5.0
    try:
        out = subprocess.run(  # noqa: PLW1510
            [git, "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def resolve_canonical_root(
    candidate: str,
    deadline: Deadline | None = None,
    catalog: list[dict] | None = None,
) -> RootResolution:
    """Resolve the canonical primary worktree for ``candidate`` (Issue #2979).

    ``candidate`` may be a linked worktree, the primary root or any directory
    inside one of them. ``catalog`` is a test seam: when supplied it replaces the
    ``list_worktrees()`` result (the module-level ``list_worktrees`` may also be
    patched). Failure kinds are returned as fixed reason-code literals and never
    silently fall back to ``candidate``:

    * ``primary_root_unresolved``    -- candidate is not a Git work tree, the
      catalog is empty, or the first catalog entry is not a usable work tree
      (bare repository / missing directory);
    * ``worktree_catalog_unavailable`` -- ``list_worktrees()`` returned ``None``;
    * ``git_common_dir_unavailable``   -- an independent ``--git-common-dir`` failed;
    * ``repository_identity_mismatch`` -- the independently obtained common-dir of
      the candidate and of the primary differ.
    """
    candidate_real = os.path.realpath(candidate)
    git = shutil.which("git")
    if not git:
        return RootResolution(False, None, WORKTREE_CATALOG_UNAVAILABLE, None, candidate_real)
    if not os.path.isdir(candidate_real) or not _is_inside_work_tree(candidate_real, git, deadline):
        return RootResolution(False, None, PRIMARY_ROOT_UNRESOLVED, None, candidate_real)

    if catalog is None:
        catalog = list_worktrees(candidate_real, deadline)
    if catalog is None:
        return RootResolution(False, None, WORKTREE_CATALOG_UNAVAILABLE, None, candidate_real)
    if not catalog:
        return RootResolution(False, None, PRIMARY_ROOT_UNRESOLVED, catalog, candidate_real)

    primary_raw = catalog[0].get("worktree_realpath")
    if not primary_raw:
        return RootResolution(False, None, PRIMARY_ROOT_UNRESOLVED, catalog, candidate_real)
    primary = os.path.realpath(primary_raw)
    # A bare primary (no work tree) or a missing directory is "primary absent".
    if not os.path.isdir(primary) or not _is_inside_work_tree(primary, git, deadline):
        return RootResolution(False, None, PRIMARY_ROOT_UNRESOLVED, catalog, candidate_real)

    candidate_common = _git_common_dir(candidate_real, git, deadline)
    primary_common = _git_common_dir(primary, git, deadline)
    if candidate_common is None or primary_common is None:
        return RootResolution(False, None, GIT_COMMON_DIR_UNAVAILABLE, catalog, candidate_real)
    if candidate_common != primary_common:
        return RootResolution(False, None, REPOSITORY_IDENTITY_MISMATCH, catalog, candidate_real)
    return RootResolution(True, primary, None, catalog, candidate_real)


def root_candidate(explicit: str | None, script_file: str) -> str:
    """Return the root CANDIDATE by priority: explicit -> CLAUDE_PROJECT_DIR -> script location.

    ``script_file`` is ``__file__`` of a script that lives in ``scripts/agent-ops``.
    The candidate is only a starting point; callers must normalise it with
    :func:`resolve_canonical_root` (a candidate is never trusted as the root).
    """
    if explicit:
        return os.path.realpath(explicit)
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return os.path.realpath(env_root)
    agent_ops = os.path.dirname(os.path.realpath(script_file))
    return os.path.realpath(os.path.dirname(os.path.dirname(agent_ops)))


def best_effort_root(
    explicit: str | None, script_file: str, deadline: Deadline | None = None
) -> str:
    """Never-raising root path: the verified primary root when resolvable, else the raw candidate.

    The raw candidate is returned on failure ONLY so that callers can proceed to
    their existing structured-refusal path, which re-runs
    :func:`resolve_canonical_root` and reports the fixed reason code. It must not
    be used as a trusted root by itself.
    """
    cand = root_candidate(explicit, script_file)
    try:
        res = resolve_canonical_root(cand, deadline)
    except Exception:  # noqa: BLE001 - best-effort path must never raise
        return cand
    return res.primary_root if res.ok and res.primary_root else cand


def find_containing_entry(catalog: list[dict], path: str) -> dict | None:
    """Return the catalog entry whose worktree contains ``path`` (Issue #2979 AC9).

    Containment is decided on realpaths. Linked worktree entries (everything
    after the first / primary entry) win over the primary because the primary
    root is a path prefix of every ``.claude/worktrees/*`` entry; among several
    linked matches the DEEPEST (longest path) wins; the primary is returned only
    when nothing else matches.
    """
    if not catalog:
        return None
    target = os.path.realpath(path)

    def _contains(entry: dict) -> bool:
        wt = entry.get("worktree_realpath")
        return bool(wt) and (target == wt or target.startswith(wt.rstrip(os.sep) + os.sep))

    linked = [e for e in catalog[1:] if _contains(e)]
    if linked:
        return max(linked, key=lambda e: len(e["worktree_realpath"]))
    return catalog[0] if _contains(catalog[0]) else None


def issue_number_from_entry(entry: dict | None) -> str | None:
    """Return the Issue number a catalog entry is bound to, or None.

    Uses the same strict rule as :func:`select_issue_worktrees`: the branch
    short-name AND the path basename must both match ``(worktree-)?issue-<N>-*``
    and agree on ``<N>``.
    """
    if not entry:
        return None
    wt = entry.get("worktree_realpath")
    if not wt:
        return None
    branch = branch_short_name(entry.get("branch_ref"))
    base = os.path.basename(os.path.normpath(wt))
    mb = re.match(r"^(?:worktree-)?issue-(\d+)-", branch or "")
    mp = re.match(r"^issue-(\d+)-", base)
    if mb and mp and mb.group(1) == mp.group(1):
        return mb.group(1)
    return None
