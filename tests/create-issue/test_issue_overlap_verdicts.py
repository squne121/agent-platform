"""AC1 / AC2: classify_overlap が title / goal / Allowed Paths / labels /
parent refs から verdict を返し、その verdict が closed enum に収まることを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio


def test_verdicts_is_closed_enum_of_four():
    assert cio.VERDICTS == {
        "duplicate",
        "overlap_requires_comment",
        "safe_new_issue",
        "ambiguous_requires_human",
    }


def test_verdict_safe_new_issue_when_no_candidates():
    current = cio.IssueScope(
        title="実装: 新しい helper を追加する",
        allowed_paths=("src/state/new_module.ts",),
    )
    result = cio.classify_overlap(current, [])
    assert result.verdict in cio.VERDICTS
    assert result.verdict == cio.SAFE_NEW_ISSUE


def test_verdict_safe_new_issue_when_paths_disjoint():
    current = cio.IssueScope(title="実装: A 機能", allowed_paths=("src/systems/a.ts",))
    candidates = [
        cio.IssueScope(
            number=10,
            title="実装: B 機能",
            allowed_paths=("src/systems/b.ts",),
            state="OPEN",
        )
    ]
    assert cio.classify_overlap(current, candidates).verdict == cio.SAFE_NEW_ISSUE


def test_verdict_duplicate_when_same_path_set():
    current = cio.IssueScope(
        title="実装: overlap helper",
        allowed_paths=(
            ".claude/skills/create-issue/scripts/check_issue_overlap.py",
            "docs/dev/workflow.md",
        ),
    )
    candidates = [
        cio.IssueScope(
            number=42,
            title="実装: overlap helper 別表現",
            allowed_paths=(
                "docs/dev/workflow.md",
                ".claude/skills/create-issue/scripts/check_issue_overlap.py",
            ),
            state="OPEN",
        )
    ]
    result = cio.classify_overlap(current, candidates)
    assert result.verdict == cio.DUPLICATE
    assert 42 in result.matched_issues


def test_verdict_overlap_requires_comment_on_partial_overlap():
    current = cio.IssueScope(
        title="実装: overlap helper",
        allowed_paths=(
            ".claude/skills/create-issue/scripts/check_issue_overlap.py",
            "tests/create-issue/test_issue_overlap_verdicts.py",
        ),
    )
    candidates = [
        cio.IssueScope(
            number=7,
            title="実装: create-issue scripts の別変更",
            allowed_paths=(".claude/skills/create-issue/scripts/check_issue_overlap.py",),
            state="OPEN",
        )
    ]
    result = cio.classify_overlap(current, candidates)
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert 7 in result.matched_issues
    assert result.overlapping_paths


def test_verdict_overlap_via_directory_prefix():
    current = cio.IssueScope(
        title="実装: tests 追加",
        allowed_paths=("tests/create-issue/test_issue_overlap_cli.py",),
    )
    candidates = [
        cio.IssueScope(
            number=8,
            title="実装: ディレクトリ単位の変更",
            allowed_paths=("tests/create-issue",),
            state="OPEN",
        )
    ]
    assert cio.classify_overlap(current, candidates).verdict == cio.OVERLAP_REQUIRES_COMMENT


def test_verdict_ambiguous_when_title_dup_but_paths_disjoint():
    current = cio.IssueScope(
        title="実装: overlap preflight を標準化する",
        allowed_paths=("src/state/x.ts",),
    )
    candidates = [
        cio.IssueScope(
            number=99,
            title="実装: overlap preflight を標準化する",
            allowed_paths=("docs/unrelated/y.md",),
            state="OPEN",
        )
    ]
    result = cio.classify_overlap(current, candidates)
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    assert 99 in result.matched_issues


def test_closed_candidate_is_ignored():
    current = cio.IssueScope(
        title="実装: overlap helper",
        allowed_paths=(".claude/skills/create-issue/scripts/check_issue_overlap.py",),
    )
    candidates = [
        cio.IssueScope(
            number=5,
            title="実装: overlap helper",
            allowed_paths=(".claude/skills/create-issue/scripts/check_issue_overlap.py",),
            state="CLOSED",
        )
    ]
    assert cio.classify_overlap(current, candidates).verdict == cio.SAFE_NEW_ISSUE
