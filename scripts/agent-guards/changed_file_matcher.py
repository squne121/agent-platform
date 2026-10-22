#!/usr/bin/env python3
"""Shared changed-file matcher / normalizer / record grammar (Issue #1611 AC11).

Extracted from
`.claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py` so
that staging (`controlled_git_change_exec.py`), commit classification
(`git_mutation_command_policy.py`), and PR review
(`allowed_paths_review_gate.py`) all evaluate Allowed Paths against the
exact same rename-aware `git diff --name-status -M -z` parsing grammar and
the exact same repo-relative path normalizer / segment matcher -- never
independently reimplemented copies that could silently drift apart from
each other.

This module is intentionally dependency-free (stdlib only) so every
consumer can `sys.path.insert` this directory and import it without extra
install steps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

# ─── Rename provenance source policy (Issue #1300, carried over verbatim) ───
#
# preferred_oracle: github_pull_request_files_api_with_previous_filename
# deterministic_local_fallback: git_diff_name_status_find_renames_z
#   (this is the operative source for staging/commit/review today.)
# insufficient_for_rename_provenance: gh_pr_diff_name_only,
#   git_diff_current_merge_base_head_name_only
# forbidden: git_diff_snapshot_base_head, post_image_filename_only_for_rename_gate

SOURCE_GIT_NAME_STATUS_Z = "git_diff_name_status_find_renames_z"
SOURCE_PR_FILES_API = "github_pull_request_files_api_with_previous_filename"
SOURCE_NAME_ONLY_INSUFFICIENT = "git_diff_current_merge_base_head_name_only"
# Issue #1611 (contract revision, P0-2): the canonical authorization oracle
# for controlled staging/commit is `git diff-index --raw --full-index -z -M`
# (mode + OID aware, rename/submodule/type-change explicit), NOT the
# name-status form above. `SOURCE_GIT_NAME_STATUS_Z` remains valid for
# review-time (`allowed_paths_review_gate.py`) human-audit-assist use, but is
# never the staging/commit authorization oracle.
SOURCE_GIT_DIFF_INDEX_RAW_Z = "git_diff_index_raw_full_index_find_renames_z"

# Submodule (gitlink) tree entry mode, per the git object model.
GITLINK_MODE = "160000"

PATH_ROLE_FILENAME = "filename"
PATH_ROLE_PREVIOUS_FILENAME = "previous_filename"

_GIT_STATUS_LETTER_MAP = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "T": "type_changed",
    "U": "unmerged",
    "X": "unknown",
    "B": "broken",
}

_PR_FILES_STATUS_MAP = {
    "added": "added",
    "removed": "removed",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "copied",
    "changed": "type_changed",
    "unchanged": "unchanged",
}


@dataclass
class ChangedFileRecord:
    """Structured changed-file record with rename/previous-path provenance.

    Represents either a `git diff --name-status -M -z` record (local
    deterministic fallback / staging-time index re-fetch) or a GitHub PR
    files API record (preferred oracle, when supplied by a caller).
    """

    path: str
    status: str
    previous_path: Optional[str]
    source: str
    provenance_complete: bool
    # Issue #1611 (contract revision, P0-2): mode/OID provenance from
    # `git diff-index --raw --full-index -z -M`. `None` for records built
    # from a source that does not carry mode/OID (e.g. the PR files API, or
    # the legacy `--name-status` parser below) -- callers that need
    # submodule-gitlink (mode 160000) classification MUST use a record
    # whose `source == SOURCE_GIT_DIFF_INDEX_RAW_Z` (`old_mode`/`new_mode`
    # populated), never infer it from `status` alone.
    old_mode: Optional[str] = None
    new_mode: Optional[str] = None
    old_oid: Optional[str] = None
    new_oid: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def is_submodule_gitlink_change(self) -> bool:
        return self.old_mode == GITLINK_MODE or self.new_mode == GITLINK_MODE


class AllowedPathsMatcher:
    """Matches repo-relative POSIX paths against the restricted Allowed Paths grammar."""

    @staticmethod
    def normalize_path(path: str) -> Optional[str]:
        """Normalize repo-relative paths and reject invalid input."""
        if not path:
            return None
        if "\\" in path:
            return None
        if path.startswith("/"):
            return None

        normalized = path[2:] if path.startswith("./") else path
        if normalized in {"", "."}:
            return None
        segments = normalized.split("/")
        if ".." in segments:
            return None
        if "" in segments:
            return None
        return normalized

    @staticmethod
    def normalize_allowed_pattern(pattern: str) -> Optional[str]:
        # Trailing-slash patterns like "src/ui/" are treated as directory prefixes
        # and normalized to "src/ui/**". Wildcard + trailing-slash is invalid.
        if pattern.endswith("/"):
            bare = pattern[:-1]
            # Reject repeated trailing slash (e.g. "src//") or wildcard + trailing-slash
            if bare.endswith("/") or "*" in bare:
                return None
            normalized_bare = AllowedPathsMatcher.normalize_path(bare)
            if normalized_bare is None:
                return None
            return normalized_bare + "/**"
        normalized = AllowedPathsMatcher.normalize_path(pattern)
        if normalized is None:
            return None
        # matcher v2 grammar: validate each segment of the pattern.
        # A segment is valid only if it is a literal (contains no '*'),
        # exactly '*' (matches one segment), or exactly '**' (matches zero
        # or more segments). Partial-segment globs such as '*.md', 'foo*',
        # '**suffix' and '***' are invalid (fail-closed).
        for segment in normalized.split("/"):
            if "*" in segment and segment not in ("*", "**"):
                return None
        return normalized

    @staticmethod
    def matches_pattern(file_path: str, pattern: str) -> bool:
        # matcher v2 grammar: segment-based matching with no external deps.
        #   literal -> exact segment match
        #   '*'     -> exactly one segment
        #   '**'    -> zero or more segments (recursive, via dynamic programming)
        # Both inputs are already normalized repo-relative POSIX paths.
        file_parts = file_path.split("/")
        pattern_parts = pattern.split("/")
        return AllowedPathsMatcher._segment_match(file_parts, pattern_parts)

    @staticmethod
    def _segment_match(file_parts: List[str], pattern_parts: List[str]) -> bool:
        n = len(file_parts)
        m = len(pattern_parts)
        # dp[i][j] is True iff file_parts[i:] matches pattern_parts[j:].
        dp = [[False] * (m + 1) for _ in range(n + 1)]
        dp[n][m] = True
        # File fully consumed: remaining pattern matches only if all '**'.
        for j in range(m - 1, -1, -1):
            if pattern_parts[j] == "**":
                dp[n][j] = dp[n][j + 1]
        for i in range(n - 1, -1, -1):
            for j in range(m - 1, -1, -1):
                segment = pattern_parts[j]
                if segment == "**":
                    # match zero segments (advance pattern) or one+ (advance file)
                    dp[i][j] = dp[i][j + 1] or dp[i + 1][j]
                elif segment == "*":
                    dp[i][j] = dp[i + 1][j + 1]
                else:
                    dp[i][j] = segment == file_parts[i] and dp[i + 1][j + 1]
        return dp[0][0]

    @staticmethod
    def is_file_allowed(file_path: str, allowed_paths: List[str]) -> bool:
        normalized_file = AllowedPathsMatcher.normalize_path(file_path)
        if normalized_file is None:
            return False

        for pattern in allowed_paths:
            normalized_pattern = AllowedPathsMatcher.normalize_allowed_pattern(pattern)
            if normalized_pattern is None:
                continue
            if AllowedPathsMatcher.matches_pattern(normalized_file, normalized_pattern):
                return True
        return False


def parse_git_diff_name_status_z(raw: str, source: str) -> List[ChangedFileRecord]:
    """Parse `git diff --name-status -M -z` (or -C) output into ChangedFileRecord list.

    NUL-separated tokens. Non-rename/copy records: "<status>\\0<path>\\0".
    Rename/copy records: "<status><score>\\0<old_path>\\0<new_path>\\0".
    Raises ValueError on malformed status, missing paths, or unknown status
    (fail-closed -- caller must treat this as indeterminate/deny, never as
    a filename-only fallback).
    """
    tokens = [tok for tok in raw.split("\0") if tok != ""]
    records: List[ChangedFileRecord] = []
    i = 0
    while i < len(tokens):
        status_token = tokens[i]
        i += 1
        if not status_token:
            raise ValueError("malformed git diff --name-status record: empty status token")
        letter = status_token[0]
        if letter in ("R", "C"):
            if i + 1 >= len(tokens):
                raise ValueError(
                    f"malformed rename/copy record for status {status_token!r}: missing old/new path"
                )
            old_path = tokens[i]
            new_path = tokens[i + 1]
            i += 2
            if not old_path or not new_path:
                raise ValueError(f"malformed rename/copy record: empty path for status {status_token!r}")
            status_name = "renamed" if letter == "R" else "copied"
            records.append(
                ChangedFileRecord(
                    path=new_path,
                    status=status_name,
                    previous_path=old_path,
                    source=source,
                    provenance_complete=True,
                )
            )
        elif letter in _GIT_STATUS_LETTER_MAP:
            if i >= len(tokens):
                raise ValueError(f"malformed record for status {status_token!r}: missing path")
            path = tokens[i]
            i += 1
            if not path:
                raise ValueError(f"malformed record: empty path for status {status_token!r}")
            records.append(
                ChangedFileRecord(
                    path=path,
                    status=_GIT_STATUS_LETTER_MAP[letter],
                    previous_path=None,
                    source=source,
                    provenance_complete=True,
                )
            )
        else:
            raise ValueError(f"unknown git diff --name-status status: {status_token!r}")
    return records


class UnsupportedPathEncodingError(ValueError):
    """Raised when a NUL-delimited raw diff-index path token cannot be
    decoded as UTF-8 (Issue #1611 contract revision, AC5). Callers MUST
    treat this fail-closed -- never silently fall back to `errors="replace"`
    / latin-1 / a best-effort decode."""


def _decode_path_token(token: bytes) -> str:
    try:
        return token.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedPathEncodingError(f"path token is not valid UTF-8: {token!r}") from exc


def parse_git_diff_index_raw_z(raw: bytes, source: str) -> List[ChangedFileRecord]:
    """Parse `git diff-index --raw --full-index -z -M <tree-ish>` (bytes)
    output into `ChangedFileRecord` list, with mode/OID provenance (Issue
    #1611 contract revision, AC3/AC4/AC5).

    This is the canonical, mode/OID-aware authorization oracle for
    controlled staging/commit -- `--name-status` (above) remains available
    for human-audit-assist use only.

    Input MUST be `bytes` (not `str`) -- the caller must invoke the
    subprocess with `text=False` so NUL-delimited path tokens are never
    passed through a lossy/replacing text decode before this parser sees
    them. Each record is:
      ":<old_mode> <new_mode> <old_oid> <new_oid> <status>[score]\\0<path>\\0"
    or, for rename/copy (`status` in `R`/`C`):
      ":<old_mode> <new_mode> <old_oid> <new_oid> <status><score>\\0<old_path>\\0<new_path>\\0"

    Raises `ValueError` (or the `UnsupportedPathEncodingError` subclass, for
    a path token that is not valid UTF-8) on any malformed/unknown record --
    fail-closed, never a filename-only or best-effort fallback.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("parse_git_diff_index_raw_z requires bytes input (text=False subprocess output)")
    tokens = [tok for tok in bytes(raw).split(b"\0") if tok != b""]
    records: List[ChangedFileRecord] = []
    i = 0
    while i < len(tokens):
        meta_token = tokens[i]
        i += 1
        try:
            meta_str = meta_token.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"malformed raw diff-index meta token (non-ascii): {meta_token!r}") from exc
        if not meta_str.startswith(":"):
            raise ValueError(f"malformed raw diff-index meta token: {meta_str!r}")
        parts = meta_str[1:].split(" ")
        if len(parts) != 5:
            raise ValueError(f"malformed raw diff-index meta token: {meta_str!r}")
        old_mode, new_mode, old_oid, new_oid, status_field = parts
        if not status_field:
            raise ValueError(f"malformed raw diff-index meta token: {meta_str!r}")
        letter = status_field[0]
        if letter in ("R", "C"):
            if i + 1 >= len(tokens):
                raise ValueError(f"malformed rename/copy raw record for status {status_field!r}: missing old/new path")
            old_path = _decode_path_token(tokens[i])
            new_path = _decode_path_token(tokens[i + 1])
            i += 2
            if not old_path or not new_path:
                raise ValueError(f"malformed rename/copy raw record: empty path for status {status_field!r}")
            status_name = "renamed" if letter == "R" else "copied"
            records.append(
                ChangedFileRecord(
                    path=new_path,
                    status=status_name,
                    previous_path=old_path,
                    source=source,
                    provenance_complete=True,
                    old_mode=old_mode,
                    new_mode=new_mode,
                    old_oid=old_oid,
                    new_oid=new_oid,
                )
            )
        elif letter in _GIT_STATUS_LETTER_MAP:
            if i >= len(tokens):
                raise ValueError(f"malformed raw record for status {status_field!r}: missing path")
            path = _decode_path_token(tokens[i])
            i += 1
            if not path:
                raise ValueError(f"malformed raw record: empty path for status {status_field!r}")
            records.append(
                ChangedFileRecord(
                    path=path,
                    status=_GIT_STATUS_LETTER_MAP[letter],
                    previous_path=None,
                    source=source,
                    provenance_complete=True,
                    old_mode=old_mode,
                    new_mode=new_mode,
                    old_oid=old_oid,
                    new_oid=new_oid,
                )
            )
        else:
            raise ValueError(f"unknown git diff-index raw status: {status_field!r}")
    return records
