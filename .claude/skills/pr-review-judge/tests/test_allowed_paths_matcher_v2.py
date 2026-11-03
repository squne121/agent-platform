#!/usr/bin/env python3
"""Regression tests for the matcher v2 grammar (segment-based mid-path ** support).

Issue #1233: allowed_paths_review_gate.py の Allowed Paths matcher を mid-path **
対応の segment-based v2 grammar に拡張する。
"""

from pathlib import Path
from unittest.mock import patch
import sys

import pytest

import_path = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(import_path))

from allowed_paths_review_gate import AllowedPathsGateEvaluator, AllowedPathsMatcher, GateStatus


def _allowed(file_path: str, pattern: str) -> bool:
    """Helper: is file_path allowed under a single pattern via the public matcher."""
    return AllowedPathsMatcher.is_file_allowed(file_path, [pattern])


# AC1: `.claude/skills/**/SKILL.md` が `.claude/skills/pr-review-judge/SKILL.md` に一致
def test_ac1_skill_md_glob_matches_direct():
    assert _allowed(".claude/skills/pr-review-judge/SKILL.md", ".claude/skills/**/SKILL.md")


# AC2: `.claude/skills/**/SKILL.md` が `.claude/skills/foo/bar/SKILL.md` に一致
def test_ac2_skill_md_glob_matches_nested():
    assert _allowed(".claude/skills/foo/bar/SKILL.md", ".claude/skills/**/SKILL.md")
    # negative: SKILL.md でないファイルは一致しない
    assert not _allowed(".claude/skills/foo/bar/OTHER.md", ".claude/skills/**/SKILL.md")


# AC3: `docs/**/README.md` が `docs/README.md` と `docs/a/b/README.md` の両方に一致（** = 0 個以上）
def test_ac3_docs_double_star_readme_zero_or_more():
    # ** が 0 segment に一致するケース
    assert _allowed("docs/README.md", "docs/**/README.md")
    # ** が複数 segment に一致するケース
    assert _allowed("docs/a/b/README.md", "docs/**/README.md")
    # 末尾ファイル名が異なれば不一致
    assert not _allowed("docs/a/b/CHANGELOG.md", "docs/**/README.md")


# AC4: `docs/*` は `docs/README.md` に一致、`docs/a/README.md` には不一致（* = ちょうど 1 segment）
def test_ac4_docs_single_star_one_segment():
    assert _allowed("docs/README.md", "docs/*")
    assert not _allowed("docs/a/README.md", "docs/*")


# AC5: `src/**` の既存挙動維持
def test_ac5_src_double_star_regression():
    # ディレクトリ直下
    assert _allowed("src/main.ts", "src/**")
    # ネストしたファイル
    assert _allowed("src/components/Button.ts", "src/**")
    # ディレクトリ自身（** が 0 segment に一致）
    assert _allowed("src", "src/**")
    # 別ツリーは不一致
    assert not _allowed("tests/main.ts", "src/**")


# AC6: partial-segment glob（`*.md`, `foo*`, `**suffix`, `***`）は invalid
def test_ac6_partial_segment_glob_invalid():
    assert AllowedPathsMatcher.normalize_allowed_pattern("*.md") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("foo*") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("**suffix") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("***") is None
    # mid-path に置いても invalid
    assert AllowedPathsMatcher.normalize_allowed_pattern("docs/*.md") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/**suffix/file.ts") is None
    # full-segment * / ** は valid
    assert AllowedPathsMatcher.normalize_allowed_pattern("docs/*") == "docs/*"
    assert AllowedPathsMatcher.normalize_allowed_pattern(".claude/skills/**/SKILL.md") == ".claude/skills/**/SKILL.md"


# AC7: invalid path（absolute, backslash, `..`, empty segment）は fail-closed
def test_ac7_invalid_path_fail_closed():
    # absolute
    assert AllowedPathsMatcher.normalize_allowed_pattern("/src/main.ts") is None
    assert AllowedPathsMatcher.normalize_path("/src/main.ts") is None
    # backslash
    assert AllowedPathsMatcher.normalize_allowed_pattern(r"src\main.ts") is None
    assert AllowedPathsMatcher.normalize_path(r"src\main.ts") is None
    # parent traversal
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/../main.ts") is None
    assert AllowedPathsMatcher.normalize_path("src/../main.ts") is None
    # empty segment
    assert AllowedPathsMatcher.normalize_allowed_pattern("src//main.ts") is None
    assert AllowedPathsMatcher.normalize_path("src//main.ts") is None
    # invalid files never match any pattern (fail-closed at is_file_allowed)
    assert not AllowedPathsMatcher.is_file_allowed("/abs/file.ts", [".claude/skills/**/SKILL.md"])
    assert not AllowedPathsMatcher.is_file_allowed(r"src\main.ts", ["src/**"])


# AC8: changed files が `.claude/skills/.../SKILL.md` のみ + Allowed Paths が
# `.claude/skills/**/SKILL.md` のとき gate が ok を返し indeterminate を返さない（evaluator レベル）
def _make_evaluator(allowed_paths):
    snapshot_args = dict(
        pr_number=1233,
        base_ref="main",
        base_sha_at_snapshot="snapshotsha",
        current_base_sha="currentbasesha",
        diff_base_sha="mergebasesha",
        head_sha="headsha",
        reviewed_head_sha="headsha",
        allowed_paths=allowed_paths,
        contract_body_sha256="contract_sha",
        contract_source_kind="issue_comment",
        contract_source_id="999",
        expected_contract_fingerprint=None,
        issue_number=1233,
    )
    snapshot = AllowedPathsGateEvaluator(**snapshot_args)
    snapshot.compute_current_merge_base_sha = lambda: snapshot.diff_base_sha
    args = dict(snapshot_args)
    args["expected_contract_fingerprint"] = snapshot.compute_contract_fingerprint()
    evaluator = AllowedPathsGateEvaluator(**args)
    evaluator.compute_current_merge_base_sha = lambda: evaluator.diff_base_sha
    return evaluator


@patch("allowed_paths_review_gate.AllowedPathsGateEvaluator.get_changed_file_records")
def test_ac8_evaluator_returns_ok_not_indeterminate(mock_get_records):
    # NOTE (Issue #1300 review Blocker 1): the canonical local-fallback
    # source is now `git diff --name-status -M -z`
    # (get_changed_file_records_from_git()), not the deprecated
    # `get_changed_files_from_git()` (--name-only) alias. This AC is about
    # the matcher v2 grammar, not rename provenance, so we patch the
    # canonical dispatch method directly with an equivalent
    # ChangedFileRecord instead of relying on the deprecated alias.
    from allowed_paths_review_gate import ChangedFileRecord, SOURCE_GIT_NAME_STATUS_Z

    mock_get_records.return_value = [
        ChangedFileRecord(
            path=".claude/skills/pr-review-judge/SKILL.md",
            status="modified",
            previous_path=None,
            source=SOURCE_GIT_NAME_STATUS_Z,
            provenance_complete=True,
        )
    ]
    evaluator = _make_evaluator(allowed_paths=[".claude/skills/**/SKILL.md"])
    result = evaluator.evaluate()
    assert result.status == GateStatus.OK.value
    assert result.status != GateStatus.INDETERMINATE.value
    assert result.violations == []


def test_regression_common_partial_segment_patterns_stay_invalid():
    assert AllowedPathsMatcher.normalize_allowed_pattern("docs/**/*.md") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("**.js") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("**/*-post.md") is None


def test_regression_src_double_star_nested_matches_zero_or_more_segments():
    assert _allowed("src/nested", "src/**/nested")
    assert _allowed("src/a/b/nested", "src/**/nested")
    assert not _allowed("tests/a/b/nested", "src/**/nested")


def test_regression_double_star_readme_matches_root_and_nested():
    assert _allowed("README.md", "**/README.md")
    assert _allowed("docs/README.md", "**/README.md")
    assert not _allowed("docs/README.txt", "**/README.md")


def test_regression_bare_double_star_matches_any_repo_relative_path():
    assert _allowed("README.md", "**")
    assert _allowed(".claude/skills/pr-review-judge/SKILL.md", "**")
    assert not _allowed("/README.md", "**")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
