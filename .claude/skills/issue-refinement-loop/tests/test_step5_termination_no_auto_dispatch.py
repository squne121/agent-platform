"""tests/test_step5_termination_no_auto_dispatch.py

Issue #2740 AC1: `SKILL.md` の `### Step 5` セクション本文に、
`run_root_transition()` を無条件実行する imperative 指示が存在しないことを、
セクション抽出 + pytest でピン留めする（grep VC ではない）。

`issue-refinement-loop` が `approved` で終了した場合、Step 5 は
`run_root_transition()` を呼び出さず、`impl-review-loop` の起動・
implementation worker の起動・実装用 worktree の作成・production code の
実装・commit/push/PR 作成のいずれも行わずに正常終了する契約に変更された
（#2740）。`impl-review-loop` の起動は、ユーザーが実装を明示的に依頼した
invocation を通じて `impl-review-loop` 自身の Root-Owned Synchronous Entry
Transition エントリゲート（`.claude/skills/impl-review-loop/steps/
preparation.md`）が `run_root_transition()` を呼び出したときにのみ行われる。
"""

from __future__ import annotations

import pathlib
import re

_SKILL_MD_PATH = pathlib.Path(__file__).parent.parent / "SKILL.md"

_STEP5_START = "### Step 5: 終了処理 (Termination)"
_STEP5_END = "## 終了レポート投稿フロー (Termination Report Publish Flow)"

# Imperative Japanese verb forms that instruct the orchestrator to run
# something "immediately"/"unconditionally" within the same turn, as the
# pre-#2740 wording did ("root/main thread は同じ turn 内で直ちに
# `run_root_transition()`...を実行し").
_IMPERATIVE_EXECUTE_PATTERNS = (
    r"直ちに\s*`?run_root_transition",
    r"run_root_transition\(\)\s*を実行し",
    r"run_root_transition\(\).*を実行する",
)


def _load_skill_md() -> str:
    assert _SKILL_MD_PATH.exists(), f"SKILL.md not found: {_SKILL_MD_PATH}"
    return _SKILL_MD_PATH.read_text(encoding="utf-8")


def _load_step5_section() -> str:
    content = _load_skill_md()
    start = content.index(_STEP5_START)
    end = content.index(_STEP5_END, start)
    return content[start:end]


def test_step5_section_exists_and_is_nonempty():
    section = _load_step5_section()
    assert len(section.strip()) > 0


def test_step5_section_explicitly_states_it_does_not_call_run_root_transition():
    """Any `run_root_transition` mention remaining inside ### Step 5 must be
    the explicit negative statement ("does not call it") introduced by
    #2740, not the old imperative "execute it now" instruction."""
    section = _load_step5_section()
    assert "run_root_transition" in section, (
        "SKILL.md's ### Step 5 section should explicitly state that it does "
        "not call run_root_transition() (#2740), for auditability"
    )
    assert re.search(r"run_root_transition\(\)`?\s*を呼び出さず", section), (
        "the run_root_transition() mention inside ### Step 5 must be phrased "
        "as an explicit negative ('does not call it'), not an imperative "
        "'execute it now' instruction"
    )


def test_step5_section_has_no_unconditional_imperative_dispatch_instruction():
    """Even if a future edit re-introduces a `run_root_transition` mention
    (e.g. in an explanatory cross-reference), it must never be phrased as an
    unconditional imperative execution instruction within Step 5 itself."""
    section = _load_step5_section()
    for pattern in _IMPERATIVE_EXECUTE_PATTERNS:
        match = re.search(pattern, section)
        assert match is None, (
            f"SKILL.md's ### Step 5 section must not contain an imperative "
            f"'execute run_root_transition() now' instruction (pattern "
            f"{pattern!r} matched {match.group(0)!r} if present)"
        )


def test_step5_section_states_approved_termination_is_not_an_alternative_approval():
    """AC2 companion check scoped to this file's Step 5 section: the
    'approved is not an alternative approval for implementation start'
    statement must live inside (or be reachable from) the Step 5 section
    itself, not only in termination-policy.md."""
    section = _load_step5_section()
    assert (
        "代替承認ではない" in section
        or "実装開始の代替" in section
    ), (
        "SKILL.md's ### Step 5 section must state that `approved` "
        "termination is not an alternative approval for implementation "
        "start (#2740 AC2)"
    )


def test_step5_section_documents_no_dispatch_side_effects():
    """The Step 5 section must explicitly enumerate that none of
    impl-review-loop launch / implementation worker launch / implementation
    worktree creation / production code implementation / commit-push-PR
    happen as part of approved termination."""
    section = _load_step5_section()
    assert "impl-review-loop" in section
    assert "worktree" in section
    assert "正常終了" in section
