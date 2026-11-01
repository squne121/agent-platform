"""AC1 強化 / AC8: goal_ref / labels / parent_refs / dependency を実際に
判定へ使い、matched_fields・policy_class・dependency_relation を固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio


def test_same_goal_ref_with_overlap_marks_goal_field():
    current = cio.IssueScope(
        title="実装: A",
        goal="overlap preflight を標準化する",
        allowed_paths=("src/x.ts",),
    )
    cand = cio.IssueScope(
        number=11,
        title="実装: B（別タイトル）",
        goal="overlap preflight を標準化する",
        allowed_paths=("src/x.ts", "src/y.ts"),
        state="OPEN",
    )
    result = cio.classify_overlap(current, [cand])
    ev = result.candidates[0]
    assert "goal_ref" in ev.matched_fields
    assert "allowed_paths" in ev.matched_fields


def test_title_and_goal_both_match_is_duplicate():
    current = cio.IssueScope(
        title="実装: overlap helper", goal="同一ゴール", allowed_paths=("src/a.ts",)
    )
    cand = cio.IssueScope(
        number=12,
        title="実装: overlap helper",
        goal="同一ゴール",
        allowed_paths=("docs/z.md",),
        state="OPEN",
    )
    result = cio.classify_overlap(current, [cand])
    assert result.verdict == cio.DUPLICATE


def test_conflicting_parent_refs_with_path_is_ambiguous():
    current = cio.IssueScope(
        title="実装: child X",
        allowed_paths=("src/shared.ts",),
        parent_refs=("#946",),
    )
    cand = cio.IssueScope(
        number=947,
        title="実装: child Y",
        allowed_paths=("src/shared.ts",),
        parent_refs=("#946",),
        state="OPEN",
    )
    result = cio.classify_overlap(current, [cand])
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    assert result.reason_code == "parent_child_collision"
    assert "parent_refs" in result.candidates[0].matched_fields


def test_dependency_predecessor_open_is_c2b_ambiguous():
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


def test_shared_meaningful_label_marks_labels_field():
    current = cio.IssueScope(
        title="実装: A",
        allowed_paths=("src/x.ts",),
        labels=("area/ai-ops", "enhancement"),
    )
    cand = cio.IssueScope(
        number=20,
        title="実装: B",
        allowed_paths=("src/x.ts",),
        labels=("area/ai-ops", "bug"),
        state="OPEN",
    )
    result = cio.classify_overlap(current, [cand])
    assert "labels" in result.candidates[0].matched_fields
    # generic label のみ共有では labels match しない
    current2 = cio.IssueScope(title="実装: A", allowed_paths=("src/x.ts",), labels=("enhancement",))
    cand2 = cio.IssueScope(
        number=21,
        title="実装: B",
        allowed_paths=("src/x.ts",),
        labels=("enhancement",),
        state="OPEN",
    )
    res2 = cio.classify_overlap(current2, [cand2])
    assert "labels" not in res2.candidates[0].matched_fields


def test_partial_overlap_emits_comment_template_and_c1():
    current = cio.IssueScope(title="実装: A", allowed_paths=("src/a.ts", "src/b.ts"))
    cand = cio.IssueScope(
        number=30,
        title="実装: 無関係タイトル zzz",
        allowed_paths=("src/b.ts",),
        state="OPEN",
    )
    result = cio.classify_overlap(current, [cand])
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert result.policy_class == "C1"
    assert result.comment_template and "#30" in result.comment_template


def test_to_dict_has_full_schema():
    current = cio.IssueScope(title="実装: A", allowed_paths=("src/a.ts",))
    d = cio.classify_overlap(current, []).to_dict()
    for key in (
        "schema_version",
        "decision",
        "reason_code",
        "policy_class",
        "source_status",
        "target",
        "candidates",
        "comment_template",
    ):
        assert key in d, f"missing key: {key}"
    assert d["schema_version"] == cio.SCHEMA_VERSION
    assert d["reason_code"] in cio.REASON_CODES
    assert d["policy_class"] in cio.POLICY_CLASSES
