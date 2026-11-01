#!/usr/bin/env python3

from pathlib import Path


SKILL_MD = Path(__file__).parent.parent / "SKILL.md"
REFERENCES_DIR = Path(__file__).parent.parent / "references"


def load_skill_md() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found: {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def test_skill_md_has_thin_entrypoint_sentinel():
    skill_md = load_skill_md()
    assert "ISSUE_REFINEMENT_LOOP_THIN_ENTRYPOINT_V1" in skill_md
    assert "planner_ssot: REFINEMENT_LOOP_PLAN_V1" in skill_md
    assert "no_prose_rejudgment: true" in skill_md


def test_skill_md_is_under_500_lines():
    line_count = len(SKILL_MD.read_text(encoding="utf-8").splitlines())
    assert line_count <= 500, f"SKILL.md should be <= 500 lines, got {line_count}"


def test_references_index_has_required_columns():
    index_md = REFERENCES_DIR / "index.md"
    assert index_md.exists(), "references/index.md should exist"
    text = index_md.read_text(encoding="utf-8")
    assert "| topic | file | loaded_when | owner | moved_from | must_not |" in text


def test_skill_md_links_required_reference_topics():
    skill_md = load_skill_md()
    required_refs = [
        "references/anchor-comment-handling.md",
        "references/web-research-routing.md",
        "references/follow-up-materialization.md",
        "references/termination-policy.md",
        "references/ac-vc-reflection.md",
        "references/scope-signal-guard.md",
    ]
    missing = [ref for ref in required_refs if ref not in skill_md]
    assert not missing, f"SKILL.md is missing required reference links: {missing}"


def test_forbidden_planner_rejudgment_fragments_are_absent():
    skill_md = load_skill_md()
    forbidden_fragments = [
        "investigation_policy.codebase_required = true",
        "investigation_policy.codebase_required = false",
        "web_research_policy.required = true",
        "web_research_policy.required = false",
        "true_if_any",
        "false_only_if",
        "policy_derivation:",
    ]
    found = [fragment for fragment in forbidden_fragments if fragment in skill_md]
    assert not found, f"Found forbidden planner re-judgment fragments: {found}"


def test_required_reference_files_exist():
    required_files = [
        "anchor-comment-handling.md",
        "scope-signal-guard.md",
        "ac-vc-reflection.md",
        "follow-up-materialization.md",
        "web-research-routing.md",
        "termination-policy.md",
    ]
    missing = [name for name in required_files if not (REFERENCES_DIR / name).exists()]
    assert not missing, f"Missing required reference files: {missing}"


def test_step0_orders_scope_rollup_before_hygiene():
    skill_md = load_skill_md()
    assert "scope rollup preflight" in skill_md
    assert "stale な `state/blocked` / `state/queued`" in skill_md
    assert skill_md.index("scope rollup preflight") < skill_md.index(
        "stale な `state/blocked` / `state/queued`"
    )


def test_loop_state_summary_references_loop_state_doc():
    """
    #1873: schemas/loop_state.schema.json was removed along with
    build_loop_state.py (routing-critical LOOP_STATE fields are no longer
    schema-validated as a standalone JSON Schema). SKILL.md must still
    reference the loop-state reference doc as the SSOT for LOOP_STATE_V1
    shape.
    """
    skill_md = load_skill_md()
    assert "references/loop-state.md" in skill_md, (
        "SKILL.md must reference references/loop-state.md"
    )


def test_scope_signal_guard_reference_keeps_tasks_md_fail_closed_routing():
    text = (REFERENCES_DIR / "scope-signal-guard.md").read_text(encoding="utf-8")
    for field in [
        "routing_target: issue_materialization",
        "fail_closed: true",
        "implementation_route_allowed: false",
    ]:
        assert field in text, f"Missing Product/Spec routing field: {field}"


def test_anchor_comment_reference_keeps_required_metadata_fields():
    text = (REFERENCES_DIR / "anchor-comment-handling.md").read_text(encoding="utf-8")
    for field in [
        "author_association:",
        "comment_updated_at:",
        "captured_at:",
        "api_url:",
        "snapshot:",
    ]:
        assert field in text, f"Missing anchor comment metadata field: {field}"
