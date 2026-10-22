"""Regression coverage for #1867: check_c8_outcome_concreteness() parent applicability.

canonical parent Issue (issue_kind: parent) does not require a `## Outcome`
section under existing_issue_readiness_v1, so C8 must return n/a for parent
bodies while implementation bodies keep the existing fail enforcement
(AC2/AC5).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
import check_issue_contract as checker  # noqa: E402


def _parent_body_no_outcome_no_vc() -> str:
    """Canonical parent body with neither `## Outcome` nor `## Verification Commands`."""
    return """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
goal_ref: "1867 regression parent body"
change_kind: workflow
```

## Summary

Parent tracker summary for #1867 regression coverage.

## Goal

Keep C4/C8 parent applicability correct.

## Acceptance Criteria

- [ ] AC1: parent readiness holds without Outcome/Verification Commands.
"""


class TestC8ParentNa:
    """AC2: canonical parent body (no Outcome, no Verification Commands) -> C8 is n/a."""

    def test_check_c8_returns_na_for_parent_kind(self):
        body = _parent_body_no_outcome_no_vc()
        result, issues = checker.check_c8_outcome_concreteness(body, "parent")
        assert result == checker.CheckResult.NA, (
            f"Expected C8 to be n/a for parent kind, got {result}. issues={issues}"
        )
        assert issues == []

    def test_run_checks_reports_c8_na_for_parent_body(self):
        body = _parent_body_no_outcome_no_vc()
        output = checker.run_checks(body, labels="", title="")
        assert output.deterministic_checks.C8_outcome_concreteness == checker.CheckResult.NA, (
            f"Expected C8 n/a in run_checks() output, got "
            f"{output.deterministic_checks.C8_outcome_concreteness}"
        )

    def test_run_checks_verdict_is_not_needs_fix_due_to_c8_for_parent_body(self):
        """AC3 (partial): C8 n/a must not push a parent verdict to needs-fix."""
        body = _parent_body_no_outcome_no_vc()
        output = checker.run_checks(body, labels="", title="")
        assert output.verdict == "approve", (
            f"Expected approve verdict for parent body with n/a C8, got {output.verdict}. "
            f"blocking_issues={output.blocking_issues}"
        )


class TestC8NonParentKindsKeepFailEnforcement:
    """C8 exemption is restricted to issue_kind == "parent"; other kinds must still fail."""

    def test_implementation_kind_still_fails_without_outcome_section(self):
        body = "## Machine-Readable Contract\n\n```yaml\nissue_kind: implementation\n```\n"
        result, issues = checker.check_c8_outcome_concreteness(body, "implementation")
        assert result == checker.CheckResult.FAIL
        assert issues

    def test_research_kind_still_fails_without_outcome_section(self):
        """Out of Scope: research の C8 挙動は変更しない（既存 fail を維持）。"""
        body = "## Machine-Readable Contract\n\n```yaml\nissue_kind: research\n```\n"
        result, issues = checker.check_c8_outcome_concreteness(body, "research")
        assert result == checker.CheckResult.FAIL
        assert issues

    def test_unknown_kind_still_fails_without_outcome_section(self):
        body = "## Machine-Readable Contract\n\n```yaml\nissue_kind: future-kind\n```\n"
        result, issues = checker.check_c8_outcome_concreteness(
            body, checker.UNKNOWN_ISSUE_KIND_SENTINEL
        )
        assert result == checker.CheckResult.FAIL
        assert issues
