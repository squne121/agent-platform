"""AC4: delivery-rollup parent の child 起票で、まだ存在しない child 同士の
Allowed Paths overlap を検査できることを検証する（classify_child_overlap）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio


def test_disjoint_children_are_safe():
    children = [
        cio.IssueScope(title="実装: child A", allowed_paths=("src/state/a.ts",)),
        cio.IssueScope(title="実装: child B", allowed_paths=("src/render/b.ts",)),
        cio.IssueScope(title="実装: child C", allowed_paths=("docs/c.md",)),
    ]
    result = cio.classify_child_overlap(children)
    assert result.verdict == cio.SAFE_NEW_ISSUE
    assert result.overlapping_pairs == ()


def test_overlapping_children_require_comment():
    children = [
        cio.IssueScope(
            title="実装: child A",
            allowed_paths=(
                "src/systems/combat.ts",
                "docs/dev/workflow.md",
            ),
        ),
        cio.IssueScope(
            title="実装: child B",
            allowed_paths=(
                "src/systems/reward.ts",
                "docs/dev/workflow.md",
            ),
        ),
    ]
    result = cio.classify_child_overlap(children)
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert len(result.overlapping_pairs) == 1
    pair = result.overlapping_pairs[0]
    assert pair.a_index == 0 and pair.b_index == 1
    assert "docs/dev/workflow.md" in pair.overlapping_paths


def test_identical_children_paths_flagged_duplicate():
    children = [
        cio.IssueScope(title="実装: child A", allowed_paths=("src/state/x.ts",)),
        cio.IssueScope(title="実装: child A 別表現", allowed_paths=("src/state/x.ts",)),
    ]
    result = cio.classify_child_overlap(children)
    assert result.verdict == cio.DUPLICATE


def test_child_overlap_via_directory_prefix():
    children = [
        cio.IssueScope(
            title="実装: child A",
            allowed_paths=("tests/create-issue",),
        ),
        cio.IssueScope(
            title="実装: child B",
            allowed_paths=("tests/create-issue/test_issue_overlap_cli.py",),
        ),
    ]
    result = cio.classify_child_overlap(children)
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert len(result.overlapping_pairs) == 1


def test_child_overlap_result_verdict_is_closed_enum():
    children = [
        cio.IssueScope(title="実装: child A", allowed_paths=("src/a.ts",)),
        cio.IssueScope(title="実装: child B", allowed_paths=("src/a.ts",)),
    ]
    result = cio.classify_child_overlap(children)
    assert result.verdict in cio.VERDICTS
    assert result.to_dict()["decision"] in cio.VERDICTS
