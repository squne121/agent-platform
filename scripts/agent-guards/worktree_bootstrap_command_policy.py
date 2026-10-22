#!/usr/bin/env python3
"""Shared policy for exact worktree bootstrap executor commands (Issue #1209)."""

from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
_AGENT_OPS_DIR = _ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

from skill_runtime_command_policy import (
    current_branch,
    is_trusted_repo_slug,
    resolve_default_branch,
    resolve_repo_slug,
)

WORKTREE_BOOTSTRAP_REASON_CODE = "worktree_bootstrap_executor_command"
WORKTREE_BOOTSTRAP_EXEC_REL = "scripts/agent-ops/worktree_bootstrap_exec.py"
WORKTREE_BOOTSTRAP_SCHEMA = "WORKTREE_BOOTSTRAP_COMMAND_POLICY_V1"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_METACHAR_RE = re.compile(r"[;&|<>`\n\r\0]")
_LEADING_ENV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)+")


@dataclass(frozen=True)
class ExactWorktreeBootstrapCommand:
    issue_number: int
    slug: str
    branch_name: str
    worktree_path: str
    base_ref: str
    argv: tuple[str, ...]
    wrapper: str | None


def _normalize_repo_relative_path(path: str) -> str | None:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    normalized = os.path.normpath(path)
    if normalized.startswith(".."):
        return None
    if normalized.split(os.sep) != [".claude", "worktrees", os.path.basename(normalized)]:
        return None
    return normalized


def _is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.fullmatch(slug or ""))


def expected_worktree_path(issue_number: int, slug: str) -> str:
    return os.path.join(".claude", "worktrees", f"issue-{issue_number}-{slug}")


def expected_branch_name(issue_number: int, slug: str) -> str:
    return f"worktree-issue-{issue_number}-{slug}"


def normalize_default_branch_ref(base_ref: str, default_branch: str) -> str | None:
    allowed = {
        default_branch,
        f"refs/heads/{default_branch}",
        f"origin/{default_branch}",
        f"refs/remotes/origin/{default_branch}",
    }
    return default_branch if base_ref in allowed else None


def parse_exact_worktree_bootstrap_command(
    command: str,
    project_root: str | None = None,
) -> ExactWorktreeBootstrapCommand | None:
    del project_root
    if not command or _METACHAR_RE.search(command) or _LEADING_ENV_RE.match(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    wrapper = None
    if tokens[0] == "rtk":
        wrapper = "rtk"
        tokens = tokens[1:]
        if not tokens or tokens[0] == "rtk":
            return None
    if tokens[:4] != ["uv", "run", "python3", WORKTREE_BOOTSTRAP_EXEC_REL]:
        return None
    if len(tokens) != 15:
        return None
    expected_flags = [
        "--issue-number",
        "--slug",
        "--branch-name",
        "--worktree-path",
        "--base-ref",
        "--json",
    ]
    if tokens[4] != expected_flags[0] or tokens[6] != expected_flags[1]:
        return None
    if tokens[8] != expected_flags[2] or tokens[10] != expected_flags[3]:
        return None
    if tokens[12] != expected_flags[4] or tokens[14] != expected_flags[5]:
        return None
    if any(
        tok.startswith(
            (
                "--issue-number=",
                "--slug=",
                "--branch-name=",
                "--worktree-path=",
                "--base-ref=",
                "--json=",
            )
        )
        for tok in tokens
    ):
        return None

    issue_token = tokens[5]
    slug = tokens[7]
    branch_name = tokens[9]
    worktree_path = tokens[11]
    base_ref = tokens[13]

    if not issue_token.isdigit() or int(issue_token) <= 0:
        return None
    issue_number = int(issue_token)
    if not _is_valid_slug(slug):
        return None
    normalized_path = _normalize_repo_relative_path(worktree_path)
    if normalized_path is None:
        return None

    expected_path = expected_worktree_path(issue_number, slug)
    expected_branch = expected_branch_name(issue_number, slug)
    if normalized_path != expected_path:
        return None
    if branch_name != expected_branch:
        return None

    return ExactWorktreeBootstrapCommand(
        issue_number=issue_number,
        slug=slug,
        branch_name=branch_name,
        worktree_path=normalized_path,
        base_ref=base_ref,
        argv=tuple(tokens),
        wrapper=wrapper,
    )


def is_exact_worktree_bootstrap_executor_command(
    command: str,
    cwd: str,
    project_root: str,
    deadline: object | None = None,
) -> bool:
    parsed = parse_exact_worktree_bootstrap_command(command, project_root)
    if parsed is None:
        return False
    if os.path.realpath(cwd) != os.path.realpath(project_root):
        return False
    default_branch = resolve_default_branch(project_root, deadline)
    if normalize_default_branch_ref(parsed.base_ref, default_branch) is None:
        return False
    if current_branch(project_root, deadline) != default_branch:
        return False
    return is_trusted_repo_slug(resolve_repo_slug(project_root, deadline))


def looks_like_worktree_bootstrap_executor_command(command: str) -> bool:
    if not command:
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    for token in tokens:
        if os.path.basename(token) == "worktree_bootstrap_exec.py":
            return True
    return False
