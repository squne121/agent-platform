"""AC7: path normalization contract（#387 互換志向）の境界を固定する。

numbered list / 連続 slash / trailing slash directory / ** glob / segment 境界 /
duplicate collapse / backtick + 注釈除去 を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio


def test_numbered_markdown_list_marker_stripped():
    assert cio.normalize_path("1. `src/state/x.ts`") == "src/state/x.ts"
    assert cio.normalize_path("2) src/state/y.ts") == "src/state/y.ts"


def test_repeated_slash_collapsed():
    assert cio.normalize_path("src//state///x.ts") == "src/state/x.ts"


def test_trailing_slash_directory_semantics():
    assert cio.normalize_path("tests/create-issue/") == "tests/create-issue"
    assert cio.paths_conflict("tests/create-issue/", "tests/create-issue/test_x.py")


def test_double_star_glob_prefix():
    assert cio.normalize_path("src/systems/**") == "src/systems"
    assert cio.normalize_path("src/systems/*") == "src/systems"
    assert cio.paths_conflict("src/systems/**", "src/systems/combat.ts")


def test_common_string_prefix_different_segment_is_not_overlap():
    # "tests/create" は "tests/create-issue" の segment 接頭辞ではない
    assert not cio.paths_conflict("tests/create", "tests/create-issue")


def test_duplicate_path_collapse():
    assert cio.normalize_paths(["src/a.ts", "./src/a.ts", "src/a.ts/"]) == ("src/a.ts",)


def test_backtick_and_annotation_stripped():
    assert cio.normalize_path("- `docs/dev/workflow.md`（参照追記）") == "docs/dev/workflow.md"
    assert cio.normalize_path("`src/x.ts` (helper)") == "src/x.ts"


def test_allowed_paths_overlap_reports_specific_path():
    overlap = cio.allowed_paths_overlap(["tests/create-issue/test_x.py"], ["tests/create-issue"])
    assert overlap == ("tests/create-issue/test_x.py",)
