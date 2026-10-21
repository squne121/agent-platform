#!/usr/bin/env python3
"""
Contract test for Issue #1478: SKILL.md Step 0f must document the
"parent-owned preflight" operating model for delegation to isolation
worktree agents.

AC1-AC4 verify the presence of specific required phrases (matching the
Issue #1478 Verification Commands rg patterns) inside SKILL.md.
"""

import re
from pathlib import Path


SKILL_MD = Path(__file__).parent.parent / "SKILL.md"


def load_skill_md() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found: {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def test_ac1_parent_owned_preflight_section_present():
    """AC1: SKILL.md contains a "parent-owned preflight" section."""
    skill_md = load_skill_md()
    assert "parent-owned preflight" in skill_md, (
        'SKILL.md Step 0f must contain a "parent-owned preflight" section '
        "describing that parent (orchestrator) runs preflight.run and passes "
        "bounded results to the isolation worktree agent."
    )


def test_ac2_isolation_agent_does_not_run_exact_executor():
    """AC2: isolation agent does not run skill_runtime_exec.py itself."""
    skill_md = load_skill_md()
    pattern = re.compile(r"isolation agent は.*skill_runtime_exec\.py.*自ら実行しない")
    assert pattern.search(skill_md), (
        "SKILL.md must state that the isolation agent does not run "
        "skill_runtime_exec.py (exact executor) itself."
    )


def test_ac2_isolation_agent_does_not_run_direct_wrapper():
    """AC2: isolation agent does not run run_refinement_preflight.py itself."""
    skill_md = load_skill_md()
    pattern = re.compile(r"isolation agent は.*run_refinement_preflight\.py.*自ら実行しない")
    assert pattern.search(skill_md), (
        "SKILL.md must state that the isolation agent does not run "
        "run_refinement_preflight.py (direct wrapper) itself."
    )


def test_ac3_agent_star_naming_pattern_not_added_as_authorization_basis():
    """AC3: generic `agent-*` worktree naming is not added as an authorization basis."""
    skill_md = load_skill_md()
    assert "認可根拠として追加しない" in skill_md, (
        "SKILL.md must state that unproven isolation worktree naming patterns "
        "such as `agent-*` are not added as an authorization basis for "
        "preflight.run."
    )


def test_ac4_direct_exec_bash_block_scope_note_present():
    """AC4: existing direct-exec bash block is scoped to orchestrator-only execution."""
    skill_md = load_skill_md()
    pattern = re.compile(r"isolation worktree agent からは.*実行しない")
    assert pattern.search(skill_md), (
        "SKILL.md must note that the existing direct run_refinement_preflight.py "
        "bash block is limited to orchestrator execution from canonical main root "
        "or a canonically-named issue worktree, and is not run directly from an "
        "isolation worktree agent."
    )


def test_parent_owned_preflight_section_is_within_step_0f():
    """The parent-owned preflight section must live inside Step 0f, not elsewhere."""
    skill_md = load_skill_md()
    assert "### Step 0f: Planner 結果の消費" in skill_md
    step_0f_start = skill_md.index("### Step 0f: Planner 結果の消費")
    step_1_start = skill_md.index("### Step 1: 事前調査 (Investigation)")
    assert step_0f_start < step_1_start
    step_0f_section = skill_md[step_0f_start:step_1_start]
    assert "parent-owned preflight" in step_0f_section, (
        'The "parent-owned preflight" section must be located within Step 0f.'
    )


def test_skill_md_stays_within_line_budget():
    """Regression guard: SKILL.md thin entrypoint line budget (max_skill_lines: 500)."""
    line_count = len(load_skill_md().splitlines())
    assert line_count <= 500, f"SKILL.md should be <= 500 lines, got {line_count}"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
