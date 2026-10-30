"""AC3: GitHub full-text search の false positive を issue body read-back で
除外できることを検証する。

search hit という事実だけでは overlap と判定せず、候補 body の ``## Allowed Paths``
セクションを read-back して実際に Allowed Paths が重なるかを確認する。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio

# 候補 body 中に "check_issue_overlap" という単語は登場するが（→ search hit）、
# 実際の Allowed Paths は別ファイルで overlap しない（false positive）。
_FALSE_POSITIVE_BODY = """## Outcome

別件で check_issue_overlap という言葉に触れているだけのコメント引用。

## Allowed Paths

- docs/dev/unrelated-note.md
"""

# 候補 body の Allowed Paths が現在の起票候補と本当に重なる（true positive）。
_TRUE_POSITIVE_BODY = """## Outcome

create-issue の helper を変更する。

## Allowed Paths

- .claude/skills/create-issue/scripts/check_issue_overlap.py
- .claude/skills/create-issue/SKILL.md
"""


def _current() -> cio.IssueScope:
    return cio.IssueScope(
        title="実装: overlap preflight を標準化する",
        allowed_paths=(".claude/skills/create-issue/scripts/check_issue_overlap.py",),
    )


def test_search_hit_without_real_overlap_is_excluded():
    candidate = cio.IssueScope(
        number=321,
        title="docs: メモ更新",
        body=_FALSE_POSITIVE_BODY,
        state="OPEN",
        search_hit=True,
    )
    result = cio.classify_overlap(_current(), [candidate])
    assert result.verdict == cio.SAFE_NEW_ISSUE
    assert 321 in result.excluded_false_positives
    assert 321 not in result.matched_issues


def test_read_back_extracts_allowed_paths_from_body():
    paths = cio.extract_allowed_paths(_TRUE_POSITIVE_BODY)
    assert ".claude/skills/create-issue/scripts/check_issue_overlap.py" in paths
    # false positive body の Allowed Paths は別ファイルのみ
    fp_paths = cio.extract_allowed_paths(_FALSE_POSITIVE_BODY)
    assert "docs/dev/unrelated-note.md" in fp_paths
    assert all("check_issue_overlap.py" not in p for p in fp_paths)


def test_search_hit_with_real_overlap_is_kept():
    candidate = cio.IssueScope(
        number=654,
        title="実装: create-issue helper を直す",
        body=_TRUE_POSITIVE_BODY,
        state="OPEN",
        search_hit=True,
    )
    result = cio.classify_overlap(_current(), [candidate])
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert 654 in result.matched_issues
    assert 654 not in result.excluded_false_positives


def test_mixed_candidates_only_false_positive_excluded():
    candidates = [
        cio.IssueScope(
            number=321,
            title="docs: メモ更新",
            body=_FALSE_POSITIVE_BODY,
            state="OPEN",
            search_hit=True,
        ),
        cio.IssueScope(
            number=654,
            title="実装: create-issue helper を直す",
            body=_TRUE_POSITIVE_BODY,
            state="OPEN",
            search_hit=True,
        ),
    ]
    result = cio.classify_overlap(_current(), candidates)
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert 321 in result.excluded_false_positives
    assert 654 in result.matched_issues
