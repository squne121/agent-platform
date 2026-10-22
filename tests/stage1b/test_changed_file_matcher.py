from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from changed_file_matcher import (  # noqa: E402
    PATH_ROLE_FILENAME,
    PATH_ROLE_PREVIOUS_FILENAME,
    SOURCE_GIT_NAME_STATUS_Z,
    AllowedPathsMatcher,
    ChangedFileRecord,
    parse_git_diff_name_status_z,
)


def test_normalize_path_rejects_invalid_input():
    assert AllowedPathsMatcher.normalize_path("") is None
    assert AllowedPathsMatcher.normalize_path("/abs/path") is None
    assert AllowedPathsMatcher.normalize_path("a\\b") is None
    assert AllowedPathsMatcher.normalize_path("..") is None
    assert AllowedPathsMatcher.normalize_path("a/../b") is None
    assert AllowedPathsMatcher.normalize_path("./a/b") == "a/b"
    assert AllowedPathsMatcher.normalize_path("a/b") == "a/b"


def test_normalize_allowed_pattern_trailing_slash_and_glob_rules():
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui/") == "src/ui/**"
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui//") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("*.md") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("foo*") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/*") == "src/*"
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/**") == "src/**"


def test_matches_pattern_segment_semantics():
    assert AllowedPathsMatcher.matches_pattern("a/b/c.py", "a/**")
    assert AllowedPathsMatcher.matches_pattern("a/c.py", "a/*")
    assert not AllowedPathsMatcher.matches_pattern("a/b/c.py", "a/*")
    assert AllowedPathsMatcher.matches_pattern("a/b/c.py", "a/*/c.py")


def test_is_file_allowed_uses_normalized_patterns():
    allowed = ["scripts/agent-guards/**", "docs/dev/hook-boundaries.md"]
    assert AllowedPathsMatcher.is_file_allowed("scripts/agent-guards/foo.py", allowed)
    assert AllowedPathsMatcher.is_file_allowed("docs/dev/hook-boundaries.md", allowed)
    assert not AllowedPathsMatcher.is_file_allowed("docs/dev/other.md", allowed)


def test_parse_git_diff_name_status_z_simple_records():
    raw = "A\0added.txt\0M\0modified.txt\0D\0removed.txt\0"
    records = parse_git_diff_name_status_z(raw, source=SOURCE_GIT_NAME_STATUS_Z)
    assert [(r.status, r.path, r.previous_path) for r in records] == [
        ("added", "added.txt", None),
        ("modified", "modified.txt", None),
        ("removed", "removed.txt", None),
    ]
    for record in records:
        assert isinstance(record, ChangedFileRecord)
        assert record.source == SOURCE_GIT_NAME_STATUS_Z
        assert record.provenance_complete is True


def test_parse_git_diff_name_status_z_rename_record_has_both_paths():
    raw = "R100\0old/path.py\0new/path.py\0"
    records = parse_git_diff_name_status_z(raw, source=SOURCE_GIT_NAME_STATUS_Z)
    assert len(records) == 1
    record = records[0]
    assert record.status == "renamed"
    assert record.path == "new/path.py"
    assert record.previous_path == "old/path.py"


def test_parse_git_diff_name_status_z_malformed_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_git_diff_name_status_z("Z\0path.txt\0", source=SOURCE_GIT_NAME_STATUS_Z)
    with pytest.raises(ValueError):
        parse_git_diff_name_status_z("R100\0only_old_path\0", source=SOURCE_GIT_NAME_STATUS_Z)


def test_path_role_constants_exposed():
    assert PATH_ROLE_FILENAME == "filename"
    assert PATH_ROLE_PREVIOUS_FILENAME == "previous_filename"
