"""
Unit tests for check_pr_review_gates.py G6: allowed_paths_gate / producer_role
block presence (Issue #1776).

These tests are split into a dedicated file (rather than appended to
test_check_pr_review_gates.py) because at contract-review time no test with
these names existed yet in the existing file; a `-k` filter against the
existing file would have hit pytest exit 5 (no tests collected), which is
not absorbable via a `# baseline-expect: fail` VC annotation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from check_pr_review_gates import CheckPRReviewGates, GateStatus  # noqa: E402


class TestG6AllowedPathsGateProducerRolePresence:
    """Tests for G6: allowed_paths_gate / producer_role presence gate."""

    def test_g6_no_pr_body_not_applicable(self):
        """G6: no PR body supplied at all -> not_applicable (no data)."""
        checker = CheckPRReviewGates()
        result = checker.g6_allowed_paths_gate_producer_role_presence(pr_body="")
        assert result.status == GateStatus.NOT_APPLICABLE.value

    def test_missing_allowed_paths_gate_block_indeterminate(self):
        """G6: PR review body without an allowed_paths_gate block -> fail
        (indeterminate / merge-blocking)."""
        pr_body = (
            "```yaml\n"
            "LOOP_VERDICT_V2:\n"
            "  verdict: APPROVE\n"
            "```\n"
            '{"producer_role": "review_subagent"}\n'
        )
        checker = CheckPRReviewGates()
        result = checker.g6_allowed_paths_gate_producer_role_presence(pr_body=pr_body)
        assert result.status == GateStatus.FAIL.value
        assert "indeterminate" in result.minimal_context
        assert "allowed_paths_gate" in result.minimal_context

    def test_missing_producer_role_indeterminate(self):
        """G6: PR review body without a producer_role field -> fail
        (indeterminate / merge-blocking)."""
        pr_body = (
            "```yaml\n"
            "LOOP_VERDICT_V2:\n"
            "  verdict: APPROVE\n"
            "  allowed_paths_gate:\n"
            "    status: ok\n"
            "```\n"
        )
        checker = CheckPRReviewGates()
        result = checker.g6_allowed_paths_gate_producer_role_presence(pr_body=pr_body)
        assert result.status == GateStatus.FAIL.value
        assert "indeterminate" in result.minimal_context
        assert "producer_role" in result.minimal_context

    def test_both_missing_indeterminate(self):
        """G6: PR review body with neither block -> fail, both reasons listed."""
        checker = CheckPRReviewGates()
        result = checker.g6_allowed_paths_gate_producer_role_presence(
            pr_body="No structured markers here at all."
        )
        assert result.status == GateStatus.FAIL.value
        assert "allowed_paths_gate" in result.minimal_context
        assert "producer_role" in result.minimal_context

    def test_both_present_pass(self):
        """G6: PR review body with both allowed_paths_gate and producer_role -> pass."""
        pr_body = (
            "```yaml\n"
            "LOOP_VERDICT_V2:\n"
            "  verdict: APPROVE\n"
            "  allowed_paths_gate:\n"
            "    status: ok\n"
            "  allowed_paths_gate_source: review_subagent\n"
            "```\n"
            "```json\n"
            '{"producer_role": "review_subagent"}\n'
            "```\n"
        )
        checker = CheckPRReviewGates()
        result = checker.g6_allowed_paths_gate_producer_role_presence(pr_body=pr_body)
        assert result.status == GateStatus.PASS.value
        assert result.minimal_context is None

    def test_g6_full_run_all_gates_verdict_request_changes(self):
        """G6 failure propagates to overall REQUEST_CHANGES via --rule all wiring."""
        checker = CheckPRReviewGates(strict=True)
        gate = checker.run_gate("g6", pr_body="no markers here")
        checker.result.gates.append(gate)
        checker.finalize_verdict()
        assert checker.result.verdict == "REQUEST_CHANGES"
