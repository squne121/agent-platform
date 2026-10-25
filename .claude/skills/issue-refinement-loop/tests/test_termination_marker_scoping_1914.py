"""
tests/test_termination_marker_scoping_1914.py

Issue #1914 fix_delta (PR #1940 adversarial review, P0-2 / P1-2):

P0-2 blocker: `SKILL.md` used to state unconditionally that `approved`
termination emits the `LOOP_HANDOFF_RESULT_V1` marker, directly
contradicting `termination-policy.md`'s newly-added delivery-rollup parent
exemption (marker must NOT be emitted for `issue_kind: parent`). This is
the exact #1890-style "entrypoint directive vs. reference contradiction"
this Issue exists to fix -- and it must be fixed as an executable
regression, not left as prose-only.

Scope Delta (Issue #1914 #1940 review): `.claude/skills/issue-refinement-loop/SKILL.md`
and this test file (`.claude/skills/issue-refinement-loop/tests/`) are edited
under this PR even though Issue #1914's Allowed Paths list only
`.claude/skills/issue-refinement-loop/references/termination-policy.md`,
`.claude/skills/issue-contract-review/scripts/run_contract_review_once.py`,
and `.claude/skills/issue-contract-review/tests/`. This is a narrowly-scoped
one-paragraph SKILL.md correction directly required to resolve P0-2 (a
contradiction with termination-policy.md that SKILL.md's own docstring
claims does not need to exist, but which the #1940 review correctly
identified as unsafe to leave unresolved) plus the executable regression
test the review explicitly requested for it.

These are policy-content assertions (regex over the markdown text), the
same style already used by test_termination_policy_contract.py and
test_skill_md_wiring.py in this directory -- there is no separate runtime
function that enforces marker emission (the #1873 renderer was retired;
orchestrator assembles the termination comment directly from these docs).

Runtime Verification Applicability: not_applicable (static content checks
only; matches Issue #1914's own Runtime Verification Applicability
section).
"""

from __future__ import annotations

import pathlib
import re

_SKILL_MD_PATH = pathlib.Path(__file__).parent.parent / "SKILL.md"
_POLICY_PATH = (
    pathlib.Path(__file__).parent.parent / "references" / "termination-policy.md"
)


def _read(path: pathlib.Path) -> str:
    assert path.exists(), f"file not found: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P0-2: SKILL.md must scope the marker rule to implementation Issues, not
# state it unconditionally.
# ---------------------------------------------------------------------------


class TestSkillMdMarkerRuleIsScopedNotUnconditional:
    def test_skill_md_marker_sentence_is_scoped_to_implementation_issue_kind(self):
        """P0-2: SKILL.md's `approved` 終了時 marker sentence must explicitly
        scope to `issue_kind: implementation`, not state the rule
        unconditionally (the pre-fix wording directly contradicted
        termination-policy.md's delivery-rollup parent exemption)."""
        content = _read(_SKILL_MD_PATH)
        match = re.search(
            r"^`approved`.*LOOP_HANDOFF_RESULT_V1.*$",
            content,
            re.MULTILINE,
        )
        assert match, "SKILL.md must contain an `approved` / LOOP_HANDOFF_RESULT_V1 sentence"
        sentence = match.group(0)
        assert "issue_kind: implementation" in sentence, (
            "SKILL.md's LOOP_HANDOFF_RESULT_V1 marker sentence must be scoped "
            "to issue_kind: implementation, matching termination-policy.md's "
            "'適用範囲' section, instead of stating the rule unconditionally."
        )

    def test_skill_md_documents_parent_exclusion_from_marker(self):
        """P0-2: SKILL.md must also document that issue_kind: parent
        (including delivery-rollup parent) is explicitly excluded from
        marker emission, consistent with termination-policy.md."""
        content = _read(_SKILL_MD_PATH)
        assert "issue_kind: parent" in content, (
            "SKILL.md must mention the issue_kind: parent exclusion from "
            "LOOP_HANDOFF_RESULT_V1 marker emission (#1914 P0-2)."
        )
        # The exclusion sentence must appear near the marker sentence (same
        # Step 5 subsection), not disconnected elsewhere in the file.
        marker_idx = content.index("LOOP_HANDOFF_RESULT_V1")
        parent_exclusion_idx = content.index("issue_kind: parent")
        assert abs(parent_exclusion_idx - marker_idx) < 2000, (
            "the issue_kind: parent exclusion must be documented adjacent to "
            "the LOOP_HANDOFF_RESULT_V1 marker sentence, not disconnected."
        )

    def test_skill_md_references_termination_policy_as_ssot(self):
        """SKILL.md must not claim its own independent authority over the
        marker rule; it must point to termination-policy.md as the SSOT
        (avoids re-introducing a second, potentially drifting copy of the
        rule)."""
        content = _read(_SKILL_MD_PATH)
        assert "termination-policy.md" in content


# ---------------------------------------------------------------------------
# P0-2 / P1-2: termination-policy.md fixes the four required cases as
# documented policy (executable regression over the policy text, matching
# the existing test_termination_policy_contract.py pattern in this file).
# ---------------------------------------------------------------------------


class TestTerminationPolicyDeliveryRollupMarkerSuppression:
    def test_implementation_approved_requires_marker(self):
        """implementation approved -> marker required."""
        content = _read(_POLICY_PATH)
        assert (
            "issue_kind: implementation" in content
            and "LOOP_HANDOFF_RESULT_V1" in content
        )
        # The scope sentence must state implementation Issues carry the marker.
        scope_match = re.search(
            r"適用範囲（implementation Issue 専用、#1914）.*", content
        )
        assert scope_match, "termination-policy.md must state the implementation-only scope of the marker"

    def test_delivery_rollup_parent_approved_forbids_marker(self):
        """delivery-rollup parent approved -> marker forbidden."""
        content = _read(_POLICY_PATH)
        assert "issue_kind: parent" in content
        assert "parent_mode: delivery-rollup" in content
        forbid_match = re.search(
            r"`LOOP_HANDOFF_RESULT_V1` marker は出力しない", content
        )
        assert forbid_match, (
            "termination-policy.md must explicitly forbid LOOP_HANDOFF_RESULT_V1 "
            "marker emission for delivery-rollup parent approved termination."
        )

    def test_delivery_rollup_parent_summary_requires_final_gate_not_applicable_and_reason_code(self):
        """delivery-rollup parent summary -> must contain
        'Final Gate: not applicable' and a reason code."""
        content = _read(_POLICY_PATH)
        assert "Final Gate: not applicable" in content
        assert "delivery_rollup_parent_without_verification_commands" in content

    def test_delivery_rollup_parent_forbids_impl_ready_and_run_impl_review_loop_signals(self):
        """delivery-rollup parent -> must NOT produce impl_ready /
        run_impl_review_loop signals."""
        content = _read(_POLICY_PATH)
        assert "impl_ready" in content
        assert "run_impl_review_loop" in content
        # Both terms must appear within the same sentence/paragraph that
        # states they are NOT emitted for the marker-suppressed case.
        section_match = re.search(
            r"marker が出力されない以上.*?run_impl_review_loop.*?(?=\n)",
            content,
            re.DOTALL,
        )
        assert section_match, (
            "termination-policy.md must explicitly state that impl_ready / "
            "run_impl_review_loop signals are not produced when the marker "
            "itself is suppressed (delivery-rollup parent approved case)."
        )

    def test_plain_markdown_summary_and_machine_readable_output_are_not_contradictory(self):
        """Plain Markdown summary ('Final Gate: not applicable') and any
        machine-readable output for the same termination must not be
        mutually contradictory -- i.e. no machine-readable field may
        simultaneously imply implementation-readiness while the summary
        says not applicable. Since the marker (which is the only
        machine-readable termination artifact for issue_kind: parent) is
        suppressed entirely, there is no machine-readable field left that
        could contradict the plain-markdown summary."""
        content = _read(_POLICY_PATH)
        assert "Final Gate 非適用" in content
        assert "Final Gate 成功" in content
        distinguish_match = re.search(
            r"「Final Gate 非適用」と「Final Gate 成功」は区別する", content
        )
        assert distinguish_match, (
            "termination-policy.md must explicitly distinguish 'Final Gate "
            "not applicable' from 'Final Gate passed' so the two are never "
            "conflated in either the plain-markdown summary or any "
            "machine-readable field."
        )
