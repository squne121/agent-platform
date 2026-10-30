"""AC4 強化 / AC9: child overlap の fixture-only 境界と fail-closed を固定する。

classify_child_overlap は #946 の child materialization gate ではなく、
sibling path overlap checker。lookup 不完全 / ambiguous child / source 失敗は
fail-closed（ambiguous_requires_human）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio

HELPER = SCRIPTS_DIR / "check_issue_overlap.py"


def _children():
    return [
        cio.IssueScope(title="実装: child A", allowed_paths=("src/a.ts",)),
        cio.IssueScope(title="実装: child B", allowed_paths=("src/a.ts",)),
    ]


def test_lookup_incomplete_is_fail_closed():
    result = cio.classify_child_overlap(_children(), lookup_complete=False)
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    assert result.policy_class == "unknown"


def test_ambiguous_child_is_fail_closed():
    result = cio.classify_child_overlap(_children(), ambiguous_child=True)
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN


def test_child_plan_source_failed_is_fail_closed():
    result = cio.classify_child_overlap(_children(), child_plan_status=cio.SOURCE_FAILED)
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN


def test_sibling_overlap_emits_comment_template():
    children = [
        cio.IssueScope(title="実装: child A", allowed_paths=("docs/dev/workflow.md", "src/a.ts")),
        cio.IssueScope(title="実装: child B", allowed_paths=("docs/dev/workflow.md", "src/b.ts")),
    ]
    result = cio.classify_child_overlap(children)
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert result.policy_class == "C1"
    assert result.comment_template and "workflow.md" in result.comment_template


def test_disjoint_children_safe():
    children = [
        cio.IssueScope(title="実装: child A", allowed_paths=("src/a.ts",)),
        cio.IssueScope(title="実装: child B", allowed_paths=("src/b.ts",)),
    ]
    assert cio.classify_child_overlap(children).verdict == cio.SAFE_NEW_ISSUE


def test_cli_children_lookup_incomplete_fail_closed(tmp_path):
    payload = {
        "lookup_complete": False,
        "children": [
            {"title": "実装: child A", "allowed_paths": ["src/a.ts"]},
            {"title": "実装: child B", "allowed_paths": ["src/a.ts"]},
        ],
    }
    f = tmp_path / "children.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(HELPER), "--title", "実装: parent", "--children-file", str(f)],
        check=True,
        capture_output=True,
        text=True,
    )
    out = json.loads(proc.stdout)
    assert out["mode"] == "child_overlap"
    assert out["decision"] == "ambiguous_requires_human"
