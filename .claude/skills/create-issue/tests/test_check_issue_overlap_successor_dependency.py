"""AC1 / AC4 (#1619): classify_overlap の successor dependency 分岐。

candidate が current に対して明示的な successor 関係
（candidate.depends_on に current の番号が含まれる = candidate が current に
依存している）を持つ場合、shared parent_refs があっても無条件の
parent_collision（C3, AMBIGUOUS_REQUIRES_HUMAN）ではなく、安全な直列化順序
として overlap_requires_comment（C2a, reason_code
successor_dependency_ordering）へ分岐することを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio  # noqa: E402


def test_dependency_successor_with_shared_parent_is_overlap_requires_comment():
    # GIVEN: current(#1611) と candidate(#1612) は shared parent_refs を持ち、
    # candidate は native blockedBy 経由で current に依存している
    # (= candidate.depends_on に current の番号が含まれる = successor 関係)
    current = cio.IssueScope(
        title="実装: current 側の変更",
        number=1611,
        allowed_paths=("src/shared.ts",),
        parent_refs=("#1600",),
    )
    cand = cio.IssueScope(
        number=1612,
        title="実装: candidate 側の変更",
        allowed_paths=("src/shared.ts",),
        parent_refs=("#1600",),
        depends_on=("#1611",),
        state="OPEN",
    )

    # WHEN
    result = cio.classify_overlap(current, [cand])

    # THEN: 無条件 parent_collision(C3) ではなく安全な C2a route になる
    assert result.candidates[0].dependency_relation.relation == "successor"
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert result.policy_class == "C2a"
    assert result.reason_code == "successor_dependency_ordering"
    assert result.reason_code != "parent_child_collision"


def test_dependency_successor_reason_code_is_in_closed_set():
    # successor_dependency_ordering は REASON_CODES の closed set に含まれる
    assert "successor_dependency_ordering" in cio.REASON_CODES


def test_dependency_successor_without_shared_parent_keeps_c1_benign_overlap():
    # GIVEN: successor 関係だが shared parent_refs がない場合は、既存の
    # C1 benign overlap（allowed_paths_overlap）のまま変更しないことを確認する
    current = cio.IssueScope(
        title="実装: current 側の変更",
        number=2001,
        allowed_paths=("src/shared.ts",),
    )
    cand = cio.IssueScope(
        number=2002,
        title="実装: 無関係タイトル zzz",
        allowed_paths=("src/shared.ts",),
        depends_on=("#2001",),
        state="OPEN",
    )

    result = cio.classify_overlap(current, [cand])

    assert result.candidates[0].dependency_relation.relation == "successor"
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert result.policy_class == "C1"
    assert result.reason_code == "allowed_paths_overlap"


def test_predecessor_behavior_is_unchanged_by_successor_branch():
    # 既存の predecessor(C2b) 分岐が successor 分岐追加の影響を受けないことを
    # 回帰確認する
    current = cio.IssueScope(
        title="実装: 後続",
        number=659,
        allowed_paths=("src/seg.ts",),
        depends_on=("#658",),
    )
    cand = cio.IssueScope(
        number=658,
        title="実装: 先行",
        allowed_paths=("src/seg.ts",),
        state="OPEN",
    )
    result = cio.classify_overlap(current, [cand])
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    assert result.policy_class == "C2b"
    assert result.candidates[0].dependency_relation.relation == "predecessor"
