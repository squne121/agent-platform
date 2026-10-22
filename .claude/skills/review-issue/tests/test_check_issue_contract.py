"""
Fixture-based tests for check_issue_contract.py

Tests the C1-C11 deterministic checks using fixture Markdown files.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema
import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
import check_issue_contract as checker  # noqa: E402

# contract_readiness_check.py is located in issue-contract-review skill
CONTRACT_READINESS_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
)
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "review_issue_result_v1.json"


def run_checker(fixture_name: str) -> dict:
    """Run the checker script on a fixture file and return parsed JSON output."""
    fixture_path = FIXTURES_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--file", str(fixture_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), (
        f"Script exited with unexpected code {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"Script output is not valid JSON: {e}\nstdout: {result.stdout}"
        ) from e


class TestPassCase:
    """GIVEN a well-formed implementation issue fixture WHEN checker runs THEN all checks pass."""

    def test_verdict_is_approve(self):
        """GIVEN pass_issue.md WHEN checker runs THEN verdict is approve."""
        output = run_checker("pass_issue.md")
        assert output["verdict"] == "approve", (
            f"Expected approve, got {output['verdict']}. "
            f"blocking_issues: {output.get('blocking_issues')}"
        )

    def test_all_deterministic_checks_pass(self):
        """GIVEN pass_issue.md WHEN checker runs THEN all C1-C11 are pass or n/a."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        allowed = {"pass", "n/a", "warn"}
        for check_id, result in checks.items():
            assert result in allowed, (
                f"Check {check_id} has unexpected result '{result}' for pass fixture"
            )

    def test_has_13_deterministic_checks(self):
        """GIVEN any fixture WHEN checker runs THEN deterministic_checks has exactly 13 keys (C1-C13)."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        assert len(checks) == 13, (
            f"Expected 13 deterministic checks (C1-C13), got {len(checks)}: {list(checks.keys())}"
        )

    def test_no_blocking_issues(self):
        """GIVEN pass_issue.md WHEN checker runs THEN blocking_issues is empty."""
        output = run_checker("pass_issue.md")
        assert output["blocking_issues"] == [], (
            f"Expected no blocking issues, got: {output['blocking_issues']}"
        )


class TestC1Fail:
    """GIVEN a fixture missing required sections WHEN checker runs THEN C1 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c1_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c1_fails(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN C1_required_sections is fail."""
        output = run_checker("c1_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C1_required_sections"] == "fail", (
            f"Expected C1 to fail, got {checks['C1_required_sections']}"
        )

    def test_c1_blocking_issue_message(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN blocking_issues contains C1 message."""
        output = run_checker("c1_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("必須セクション" in msg or "Runtime Verification" in msg for msg in blocking), (
            f"Expected C1 blocking message in {blocking}"
        )


class TestC7Fail:
    """GIVEN a fixture with workflow skills in Required Skills WHEN checker runs THEN C7 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c7_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c7_fails(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN C7_required_skills_semantics is fail."""
        output = run_checker("c7_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C7_required_skills_semantics"] == "fail", (
            f"Expected C7 to fail, got {checks['C7_required_skills_semantics']}"
        )

    def test_c7_blocking_message_mentions_workflow_skill(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN blocking_issues mentions workflow skill."""
        output = run_checker("c7_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("ワークフロースキル" in msg or "implement-issue" in msg for msg in blocking), (
            f"Expected workflow skill message in {blocking}"
        )


class TestC9Fail:
    """GIVEN a fixture missing Runtime Verification Applicability WHEN checker runs THEN C9 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c9_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c9_fails(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN C9_runtime_applicability_present is fail or legacy_missing."""
        output = run_checker("c9_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C9_runtime_applicability_present"] in ("fail", "legacy_missing_applicability"), (
            f"Expected C9 to fail or legacy_missing, got {checks['C9_runtime_applicability_present']}"
        )

    def test_c9_blocking_message(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN blocking_issues contains C9 message."""
        output = run_checker("c9_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("Runtime Verification" in msg or "Applicability" in msg or "レガシー" in msg for msg in blocking), (
            f"Expected C9 blocking message in {blocking}"
        )


class TestC9StructuredBlocker:
    """GIVEN c9_fail_issue.md (FAIL/LEGACY_MISSING) WHEN checker runs THEN structured_blockers carries a
    C9 deterministic entry with self-checker evidence; GIVEN a WARN-path body THEN no C9 entry is added.
    """

    def test_c9_fail_emits_structured_blocker_code(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN structured_blockers contains an entry whose
        code is "C9" (filtered by code/domain key, not positional index). Corresponds to Issue #1314 AC1.
        """
        output = run_checker("c9_fail_issue.md")
        c9_blockers = [
            b
            for b in output["structured_blockers"]
            if b["code"] == "C9" and b["deterministic_domain_key"] == "runtime_applicability"
        ]
        assert c9_blockers, f"Expected a C9 structured blocker, got: {output['structured_blockers']}"

    def test_c9_structured_blocker_evidence_is_valid_deterministic_evidence(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN the C9 structured_blocker entry has
        finding_kind == "deterministic_domain_blocker" and its checker_evidence contains all required
        keys. Corresponds to Issue #1314 AC2.
        """
        output = run_checker("c9_fail_issue.md")
        c9_blockers = [
            b
            for b in output["structured_blockers"]
            if b["code"] == "C9" and b["deterministic_domain_key"] == "runtime_applicability"
        ]
        assert c9_blockers, f"Expected a C9 structured blocker, got: {output['structured_blockers']}"
        blocker = c9_blockers[0]
        assert blocker["finding_kind"] == "deterministic_domain_blocker"
        assert blocker["blocking"] is True
        assert blocker["checker_evidence"], "Expected non-empty checker_evidence for C9 deterministic blocker"
        evidence = blocker["checker_evidence"][0]
        for key in (
            "source_check",
            "rule_id",
            "category",
            "artifact_path",
            "artifact_schema",
            "body_sha256",
            "iteration_id",
        ):
            assert key in evidence, f"Missing checker_evidence key: {key}"
        assert evidence["rule_id"] == "C9_runtime_applicability_present"
        assert evidence["category"] == "runtime_applicability"
        assert evidence["body_sha256"] == output["body_sha256"]

    def test_c9_findings_entry_is_deterministic_domain_blocker(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN the runtime_applicability finding is a blocking
        deterministic_domain_blocker (not a heuristic_concern).
        """
        output = run_checker("c9_fail_issue.md")
        findings = [f for f in output["findings"] if f["deterministic_domain_key"] == "runtime_applicability"]
        assert findings, "Expected runtime_applicability finding"
        assert findings[0]["finding_kind"] == "deterministic_domain_blocker"
        assert findings[0]["blocking"] is True

    def test_c9_warn_does_not_emit_structured_blocker(self):
        """GIVEN a research issue missing RVA (WARN path) WHEN checker runs THEN no C9 structured_blocker
        is added to structured_blockers.
        """
        body = """## Machine-Readable Contract
```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "research"
change_kind: docs
```

## Outcome
調査結果が記録される。

## Stop Conditions
- [ ] stop 1
- [ ] stop 2
- [ ] stop 3
- [ ] stop 4
- [ ] stop 5
- [ ] stop 6
"""
        result = checker.run_checks(body, labels="", title="research issue")
        payload = checker.result_to_dict(result)
        c9_blockers = [
            b
            for b in payload["structured_blockers"]
            if b["code"] == "C9" and b["deterministic_domain_key"] == "runtime_applicability"
        ]
        assert c9_blockers == [], f"Expected no C9 structured_blocker on WARN path, got: {c9_blockers}"


class TestC11Fail:
    """GIVEN a fixture with decision: immediate but no runtime-verification tags WHEN checker runs THEN C11 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c11_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c11_fails(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN C11_decision_tag_consistency is fail."""
        output = run_checker("c11_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C11_decision_tag_consistency"] == "fail", (
            f"Expected C11 to fail, got {checks['C11_decision_tag_consistency']}"
        )

    def test_c11_blocking_message(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN blocking_issues contains C11 message."""
        output = run_checker("c11_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("immediate" in msg or "runtime-verification" in msg for msg in blocking), (
            f"Expected C11 blocking message in {blocking}"
        )


class TestJsonOutputStructure:
    """GIVEN any fixture WHEN checker runs with --json THEN output matches expected schema."""

    def test_json_has_required_keys(self):
        """GIVEN pass_issue.md WHEN checker runs THEN JSON has verdict, deterministic_checks, issue_kind, generated_at,
            etc.
        """
        output = run_checker("pass_issue.md")
        required_keys = {(
            "verdict"
        ), "deterministic_checks", "blocking_issues", "non_blocking_improvements", "issue_kind", "generated_at"}
        missing = required_keys - set(output.keys())
        assert not missing, f"JSON output missing keys: {missing}"

    def test_all_c1_to_c11_keys_present(self):
        """GIVEN pass_issue.md WHEN checker runs THEN all C1-C11 keys are in deterministic_checks."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        expected_keys = {
            "C1_required_sections",
            "C2_stop_conditions_6",
            "C3_ac_checkbox_format",
            "C4_vc_commands_present",
            "C5_ac_vc_number_alignment",
            "C6_no_subjective_phrasing",
            "C7_required_skills_semantics",
            "C8_outcome_concreteness",
            "C9_runtime_applicability_present",
            "C10_deferred_destination_present",
            "C11_decision_tag_consistency",
        }
        missing = expected_keys - set(checks.keys())
        assert not missing, f"deterministic_checks missing keys: {missing}"


class TestC2Fail:
    """GIVEN a fixture with fewer than 6 stop conditions WHEN checker runs THEN C2 fails."""

    def test_c2_fail(self):
        """GIVEN c2_fail_issue.md WHEN checker runs THEN C2 is fail."""
        output = run_checker("c2_fail_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            f"Expected C2 to fail, got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c2_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c2_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC2StopConditionsBulletMarkers:
    """GIVEN Stop Conditions sections using non-hyphen bullet markers
    WHEN checker runs THEN C2 counts top-level bullets consistently for all
    allowed markers (-, *, +), via iter_top_level_stop_condition_items
    (Issue #1431)."""

    def test_c2_stop_conditions_asterisk_bullet(self):
        """GIVEN c2_asterisk_pass_issue.md (6 asterisk bullets) WHEN checker runs
        THEN C2_stop_conditions_6 is pass."""
        output = run_checker("c2_asterisk_pass_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "pass", (
            f"Expected C2 to pass, got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )

    def test_c2_stop_conditions_plus_bullet(self):
        """GIVEN c2_plus_pass_issue.md (6 plus bullets) WHEN checker runs
        THEN C2_stop_conditions_6 is pass."""
        output = run_checker("c2_plus_pass_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "pass", (
            f"Expected C2 to pass, got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )

    def test_c2_stop_conditions_insufficient(self):
        """GIVEN c2_asterisk_insufficient_issue.md (3 asterisk bullets, <6) WHEN checker
        runs THEN C2_stop_conditions_6 is fail."""
        output = run_checker("c2_asterisk_insufficient_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            f"Expected C2 to fail, got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )


class TestC2StopConditionsTopLevelTokenizer:
    """GIVEN Stop Conditions sections exercising the dedicated top-level
    tokenizer (iter_top_level_stop_condition_items, Issue #1431 review
    remediation) WHEN checker runs THEN C2 counts only genuine top-level
    items, excluding fence content / nested bullets / thematic breaks /
    empty items / tab-indented lines."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "c2_hyphen_pass_issue.md",
            "c2_asterisk_pass_issue.md",
            "c2_plus_pass_issue.md",
        ],
    )
    def test_marker_matrix_6_items_pass(self, fixture_name):
        """GIVEN 6 top-level items using -, *, or + markers WHEN checker runs
        THEN C2_stop_conditions_6 is pass."""
        output = run_checker(fixture_name)
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "pass", (
            f"{fixture_name}: expected C2 to pass, "
            f"got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "c2_hyphen_insufficient_issue.md",
            "c2_asterisk_insufficient_5_issue.md",
            "c2_plus_insufficient_issue.md",
        ],
    )
    def test_marker_matrix_5_items_fail(self, fixture_name):
        """GIVEN 5 top-level items using -, *, or + markers WHEN checker runs
        THEN C2_stop_conditions_6 is fail, verdict is needs-fix, and
        blocking_issues references Stop Conditions."""
        output = run_checker(fixture_name)
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            f"{fixture_name}: expected C2 to fail, "
            f"got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )
        assert output["verdict"] == "needs-fix", (
            f"{fixture_name}: expected verdict needs-fix, got {output['verdict']}"
        )
        assert any("Stop Conditions" in msg for msg in output["blocking_issues"]), (
            f"{fixture_name}: expected a Stop Conditions message in blocking_issues, "
            f"got {output['blocking_issues']}"
        )

    def test_fenced_bullet_like_lines_not_counted(self):
        """GIVEN 5 real top-level items plus a code fence containing
        bullet-like lines WHEN checker runs THEN the fenced lines are NOT
        counted (count stays 5, C2 fails) and blocking_issues references
        Stop Conditions."""
        output = run_checker("c2_fence_bullet_excluded_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            "Expected C2 to fail (fenced bullet-like lines must not be "
            f"counted), got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )
        assert output["verdict"] == "needs-fix", (
            f"Expected verdict needs-fix, got {output['verdict']}"
        )
        assert any("Stop Conditions" in msg for msg in output["blocking_issues"]), (
            f"Expected a Stop Conditions message in blocking_issues, got {output['blocking_issues']}"
        )

    def test_nested_bullet_not_counted_as_top_level(self):
        """GIVEN 5 top-level items and 1 nested (indented) sub-item WHEN
        checker runs THEN the nested item is NOT counted as top-level
        (count stays 5, C2 fails) and blocking_issues references Stop
        Conditions."""
        output = run_checker("c2_nested_bullet_excluded_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            "Expected C2 to fail (nested bullet must not be counted as "
            f"top-level), got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )
        assert output["verdict"] == "needs-fix", (
            f"Expected verdict needs-fix, got {output['verdict']}"
        )
        assert any("Stop Conditions" in msg for msg in output["blocking_issues"]), (
            f"Expected a Stop Conditions message in blocking_issues, got {output['blocking_issues']}"
        )

    def test_thematic_break_not_counted(self):
        """GIVEN 5 top-level items and a GFM thematic break line ("* * *")
        WHEN checker runs THEN the thematic break is NOT counted as a
        bullet (count stays 5, C2 fails) and blocking_issues references
        Stop Conditions."""
        output = run_checker("c2_thematic_break_excluded_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            "Expected C2 to fail (thematic break must not be counted), "
            f"got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )
        assert output["verdict"] == "needs-fix", (
            f"Expected verdict needs-fix, got {output['verdict']}"
        )
        assert any("Stop Conditions" in msg for msg in output["blocking_issues"]), (
            f"Expected a Stop Conditions message in blocking_issues, got {output['blocking_issues']}"
        )

    def test_whitespace_only_item_not_counted(self):
        """GIVEN 5 real top-level items and 1 item whose content is
        whitespace-only WHEN checker runs THEN the empty item is NOT
        counted (count stays 5, C2 fails) and blocking_issues references
        Stop Conditions."""
        output = run_checker("c2_empty_item_excluded_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            "Expected C2 to fail (whitespace-only item must not be "
            f"counted), got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )
        assert output["verdict"] == "needs-fix", (
            f"Expected verdict needs-fix, got {output['verdict']}"
        )
        assert any("Stop Conditions" in msg for msg in output["blocking_issues"]), (
            f"Expected a Stop Conditions message in blocking_issues, got {output['blocking_issues']}"
        )

    def test_tab_indented_item_rejected(self):
        """GIVEN 5 real top-level items and 1 tab-indented bullet-like line
        WHEN checker runs THEN the tab-indented line is REJECTED (not
        counted at all — tabs are not accepted as marker indentation),
        count stays 5, C2 fails, and blocking_issues references Stop
        Conditions."""
        output = run_checker("c2_tab_indent_rejected_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            "Expected C2 to fail (tab-indented line must be rejected, not "
            f"counted), got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )
        assert output["verdict"] == "needs-fix", (
            f"Expected verdict needs-fix, got {output['verdict']}"
        )
        assert any("Stop Conditions" in msg for msg in output["blocking_issues"]), (
            f"Expected a Stop Conditions message in blocking_issues, got {output['blocking_issues']}"
        )


class TestIterTopLevelStopConditionItemsUnit:
    """GIVEN small in-memory Stop Conditions section strings WHEN
    iter_top_level_stop_condition_items() runs THEN it yields exactly the
    top-level items (unit-level coverage complementing the fixture-based
    C2 tests above, Issue #1431)."""

    def test_simple_hyphen_list(self):
        section = "- a\n- b\n- c\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "b",
            "c",
        ]

    def test_fence_content_excluded(self):
        section = "- a\n- b\n```\n- fake1\n- fake2\n```\n- c\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "b",
            "c",
        ]

    def test_nested_item_excluded(self):
        section = "- a\n- b\n  - nested\n- c\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "b",
            "c",
        ]

    def test_thematic_break_excluded(self):
        section = "- a\n- b\n- c\n* * *\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "b",
            "c",
        ]

    def test_thematic_break_dash_variant_excluded(self):
        section = "- a\n- b\n- c\n- - -\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "b",
            "c",
        ]

    def test_empty_item_excluded(self):
        section = "- a\n- b\n-  \n- c\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "b",
            "c",
        ]

    def test_tab_after_marker_rejected(self):
        section = "- a\n-\tb\n- c\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "c",
        ]

    def test_tab_before_marker_rejected(self):
        section = "- a\n\t- b\n- c\n"
        assert list(checker.iter_top_level_stop_condition_items(section)) == [
            "a",
            "c",
        ]


class TestC3Fail:
    """GIVEN a fixture with non-checkbox AC WHEN checker runs THEN C3 fails."""

    def test_c3_fail(self):
        """GIVEN c3_fail_issue.md WHEN checker runs THEN C3 is fail."""
        output = run_checker("c3_fail_issue.md")
        assert output["deterministic_checks"]["C3_ac_checkbox_format"] == "fail", (
            f"Expected C3 to fail, got {output['deterministic_checks']['C3_ac_checkbox_format']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c3_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c3_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC4Fail:
    """GIVEN a fixture with VC code block but no executable commands WHEN checker runs THEN C4 fails."""

    def test_c4_fail(self):
        """GIVEN c4_fail_issue.md WHEN checker runs THEN C4 is fail."""
        output = run_checker("c4_fail_issue.md")
        assert output["deterministic_checks"]["C4_vc_commands_present"] == "fail", (
            f"Expected C4 to fail, got {output['deterministic_checks']['C4_vc_commands_present']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c4_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c4_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC5Fail:
    """GIVEN a fixture where AC2 has no corresponding VC reference WHEN checker runs THEN C5 fails."""

    def test_c5_fail(self):
        """GIVEN c5_fail_issue.md WHEN checker runs THEN C5 is fail."""
        output = run_checker("c5_fail_issue.md")
        assert output["deterministic_checks"]["C5_ac_vc_number_alignment"] == "fail", (
            f"Expected C5 to fail, got {output['deterministic_checks']['C5_ac_vc_number_alignment']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c5_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c5_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c5_deterministic_failure_emits_blocking_finding_with_evidence(self):
        """GIVEN c5_fail_issue.md WHEN checker runs THEN deterministic finding stays blocking with self-evidence."""
        output = run_checker("c5_fail_issue.md")
        findings = [f for f in output["findings"] if f["deterministic_domain_key"] == "vc_number_alignment"]
        assert findings, "Expected vc_number_alignment finding"
        assert findings[0]["finding_kind"] == "deterministic_domain_blocker"
        assert findings[0]["blocking"] is True
        assert findings[0]["checker_evidence"][0]["source_check"] == "check_issue_contract"
        assert output["structured_blockers"][0]["code"] == "C5"


class TestC6Fail:
    """GIVEN a fixture with subjective phrasing in AC WHEN checker runs THEN C6 fails."""

    def test_c6_fail(self):
        """GIVEN c6_fail_issue.md WHEN checker runs THEN C6 is fail."""
        output = run_checker("c6_fail_issue.md")
        assert output["deterministic_checks"]["C6_no_subjective_phrasing"] == "fail", (
            f"Expected C6 to fail, got {output['deterministic_checks']['C6_no_subjective_phrasing']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c6_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c6_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC6WordBoundary:
    """GIVEN AC/VC text with identifier-like tokens WHEN C6 runs THEN it does not
    flag word-boundary-safe patterns as subjective phrasing.
    """

    def test_improvement_candidate_identifier_not_flagged(self):
        """GIVEN AC/VC text containing 'agent_improvement_candidate' identifier
        WHEN C6 runs THEN result is pass (no false positive).
        """
        body = (
            "## Acceptance Criteria\n"
            "- [ ] AC1: `agent_improvement_candidate_v1.schema.json` is generated\n\n"
            "## Verification Commands\n"
            "```bash\n"
            "rg -n agent_improvement_candidate schema/\n"
            "```\n"
        )
        result, findings = checker.check_c6_no_subjective_phrasing(body)
        assert result == checker.CheckResult.PASS, (
            f"Expected PASS for identifier-only text, got {result} with findings={findings}"
        )


class TestC8Fail:
    """GIVEN a fixture with vague Outcome WHEN checker runs THEN C8 fails."""

    def test_c8_fail(self):
        """GIVEN c8_fail_issue.md WHEN checker runs THEN C8 is fail."""
        output = run_checker("c8_fail_issue.md")
        assert output["deterministic_checks"]["C8_outcome_concreteness"] == "fail", (
            f"Expected C8 to fail, got {output['deterministic_checks']['C8_outcome_concreteness']}"
        )


class TestFindingsContract:
    def test_c4_deterministic_failure_emits_blocking_finding_with_evidence(self):
        """GIVEN c4_fail_issue.md WHEN checker runs THEN deterministic blocker is preserved with self-evidence."""
        output = run_checker("c4_fail_issue.md")
        findings = [f for f in output["findings"] if f["deterministic_domain_key"] == "vc_command_format"]
        assert findings, "Expected vc_command_format finding"
        assert findings[0]["finding_kind"] == "deterministic_domain_blocker"
        assert findings[0]["blocking"] is True
        assert findings[0]["checker_evidence"][0]["rule_id"] == "C4_vc_commands_present"
        assert output["structured_blockers"][0]["code"] == "C4"

    def test_structured_blockers_are_required_and_consistent(self):
        """GIVEN c4_fail_issue.md WHEN checker runs THEN structured_blockers mirror deterministic findings."""
        output = run_checker("c4_fail_issue.md")
        structured = output["structured_blockers"]
        assert structured, "Expected structured_blockers to be populated"
        blocker = structured[0]
        assert blocker["finding_kind"] == "deterministic_domain_blocker"
        assert blocker["deterministic_domain_key"] == "vc_command_format"
        assert blocker["blocking"] is True
        assert blocker["checker_evidence"][0]["body_sha256"] == output["body_sha256"]

    def test_schema_requires_top_level_structured_blockers(self):
        """GIVEN review_issue_result_v1 schema WHEN loaded THEN structured_blockers is top-level required and
            schema-bound.
        """
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert "structured_blockers" in schema["required"]
        assert schema["properties"]["structured_blockers"]["items"]["$ref"] == "#/$defs/structured_blocker"
        assert "structured_blocker" in schema["$defs"]

    def test_schema_rejects_non_blocking_structured_blockers(self):
        """GIVEN structured_blockers entry using checker_gap WHEN schema validates THEN it is rejected."""
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        payload = run_checker("c4_fail_issue.md")
        payload["structured_blockers"] = [
            {
                "code": "C4",
                "message": "gap",
                "finding_kind": "checker_gap",
                "deterministic_domain_key": "vc_command_format",
                "blocking": False,
                "checker_evidence": [],
            }
        ]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=payload, schema=schema)

    def test_c9_warn_does_not_duplicate_findings(self):
        """GIVEN research issue missing RVA WHEN checker runs THEN warning finding is emitted once."""
        body = """## Machine-Readable Contract
```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "research"
change_kind: docs
```

## Outcome
調査結果が記録される。

## Stop Conditions
- [ ] stop 1
- [ ] stop 2
- [ ] stop 3
- [ ] stop 4
- [ ] stop 5
- [ ] stop 6
"""
        result = checker.run_checks(body, labels="", title="research issue")
        payload = checker.result_to_dict(result)
        findings = [
            f
            for f in payload["findings"]
            if f["deterministic_domain_key"] == "runtime_applicability"
        ]
        assert len(findings) == 1, f"Expected one runtime_applicability finding, got: {findings}"

    def test_top_level_json_array_becomes_structured_blocker(self, monkeypatch):
        """GIVEN the product spec checker returns a JSON array WHEN review-issue consumes it THEN it fails closed."""
        monkeypatch.setattr(checker, "_run_product_spec_check", lambda body, body_file_path: ([{"bad": True}], 0, None))

        body = (FIXTURES_DIR / "pass_issue.md").read_text(encoding="utf-8")
        result = checker.run_checks(body, labels="phase/implementation", title="実装: pass")
        payload = checker.result_to_dict(result)

        assert payload["verdict"] == "needs-fix"
        assert any(
            blocker["code"] == "PRODUCT_SPEC" and "payload_not_object" in blocker["message"]
            for blocker in payload["structured_blockers"]
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c8_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c8_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC10Fail:
    """GIVEN a fixture with decision: deferred but no destination WHEN checker runs THEN C10 fails."""

    def test_c10_fail(self):
        """GIVEN c10_fail_issue.md WHEN checker runs THEN C10 is fail."""
        output = run_checker("c10_fail_issue.md")
        assert output["deterministic_checks"]["C10_deferred_destination_present"] == "fail", (
            f"Expected C10 to fail, got {output['deterministic_checks']['C10_deferred_destination_present']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c10_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c10_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestJsonOutputSchema:
    """GIVEN any fixture WHEN checker runs THEN JSON output has all required fields."""

    def test_json_output_schema(self):
        """GIVEN pass_issue.md WHEN checker runs THEN JSON has verdict, deterministic_checks, blocking_issues,
            non_blocking_improvements, issue_kind, generated_at.
        """
        output = run_checker("pass_issue.md")
        required_fields = {(
            "verdict"
        ), "deterministic_checks", "blocking_issues", "non_blocking_improvements", "issue_kind", "generated_at"}
        missing = required_fields - set(output.keys())
        assert not missing, f"JSON output missing fields: {missing}"


class TestMachineReadableContractPriority:
    """GIVEN an issue with Machine-Readable Contract WHEN checker runs THEN issue_kind from contract takes priority."""

    def test_machine_readable_contract_priority(self):
        """GIVEN c2_fail_issue.md with issue_kind: implementation in contract WHEN checker runs THEN issue_kind is
            implementation.
        """
        # c2_fail_issue.md has issue_kind: implementation in the Machine-Readable Contract
        # and no labels/title prefix, so it should be detected from the contract
        output = run_checker("c2_fail_issue.md")
        assert output["issue_kind"] == "implementation", (
            f"Expected issue_kind=implementation from Machine-Readable Contract, got {output['issue_kind']}"
        )


# ---------------------------------------------------------------------------
# contract_readiness_execute integration tests (AC3, AC4, AC5, AC7, AC8, AC9)
# ---------------------------------------------------------------------------

# Inline fixture for human_judgment detection tests.
#
# Issue #2397 Scope Delta iteration 2: this fixture previously used a VC
# calling `nonexistent-tool-xyz-abc`, a binary not present on
# baseline_vc_preflight.py's closed command allowlist (`_ALLOWED_COMMANDS`).
# Per Issue #514's allowlist-closed policy, such a call is rejected *before*
# execution (`category: command_not_allowed`, exit_code=None), never actually
# reaching the point of executing and getting a real exit 127. Issue #2397
# Scope Delta AC9 intentionally maps `command_not_allowed -> needs_fix` in
# `_PREFLIGHT_CATEGORY_TO_READINESS` (it is a body-fixable state: the VC can
# be rewritten to use an allowlisted command), so this fixture's prior VC no
# longer exercises a genuinely operator-only (human_judgment) state.
#
# The fixture below is replaced with the same genuinely operator-only
# pattern established by Issue #2397's own new tests
# (`test_operator_intervention_route.py` /
# `test_operator_intervention_route_scope_delta.py`): `jq` IS on the closed
# allowlist, so the real checker chain actually executes it, against a path
# that does not exist. `jq`'s exit code (2) and stderr do not match any of
# `classify_result()`'s specific patterns, so classification falls through
# to the terminal "Unknown: cannot classify" branch
# (`decision: "human_judgment"`, `category: "unknown"`), which
# `map_preflight_result_to_errors()` treats as `human_judgment`
# unconditionally -- a genuine operator-only signal, unaffected by the
# `command_not_allowed -> needs_fix` fix.
# The key property (unchanged): overall status == "human_judgment"
# (or "blocked"), NOT "needs_fix".
_HUMAN_JUDGMENT_ISSUE_CONTENT = """\
---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: human_judgment テスト用フィクスチャ（VC が unknown 分類で human_judgment になる Issue）
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "human_judgment テスト用フィクスチャ"
change_kind: code
```

## Parent Issue

none

## Outcome

`human_judgment_issue.md` が baseline_vc_preflight で human_judgment 判定されることを確認する。

## Background

このフィクスチャは `--mode execute` の human_judgment 検出テスト用。
VC の `$ jq '.' fixture/e2e_check_issue_contract_human_judgment_unknown.json` は、
`jq` 自体は preflight の許可コマンド一覧（`_ALLOWED_COMMANDS`）に含まれるため実際に実行されるが、
参照先パスが存在しないため exit code 2 で失敗し、`classify_result()` の既知パターン
（env_missing_dep / timeout 等）のいずれにも一致せず `category: unknown` に分類される。
`unknown` は `_PREFLIGHT_CATEGORY_TO_READINESS` で human_judgment のまま維持される、
本文修正では解消しない genuine operator-only な状態である
（Issue #2397 Scope Delta: `command_not_allowed` を human_judgment fixture に使わない）。

## Parent Goal Ref

- Goal: human_judgment テスト用
- Desired Destination: human_judgment が検出されること

## Current Validated Scope

- テストフィクスチャのみ

## Remaining Parent Gaps

なし

## In Scope

- テストフィクスチャのみ

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `jq '.' fixture/e2e_check_issue_contract_human_judgment_unknown.json` が
      存在しないパスに対する `unknown` 分類で human_judgment となること（human_judgment テスト用）

## Verification Commands

```bash
# AC1
$ jq '.' fixture/e2e_check_issue_contract_human_judgment_unknown.json
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: テストフィクスチャ用。

## Allowed Paths

- `.claude/skills/review-issue/tests/test_check_issue_contract.py`
"""

# Inline fixture for unexpected_pass detection tests.
# This issue has a VC of `$ true` which always returns exit 0 at baseline,
# triggering unexpected_pass classification.
# Previously stored as fixtures/unexpected_pass_issue.md; inlined here so the
# fixtures/ directory does not need to be tracked for this AC.
_UNEXPECTED_PASS_ISSUE_CONTENT = """\
---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: unexpected_pass テスト用フィクスチャ（VC が baseline で pass する Issue）
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "unexpected_pass テスト用フィクスチャ"
change_kind: code
```

## Parent Issue

none

## Outcome

`unexpected_pass_issue.md` が baseline_vc_preflight で unexpected_pass 判定されることを確認する。

## Background

このフィクスチャは `--mode execute` の unexpected_pass 検出テスト用。
VC の `$ true` は実装前でも常に exit 0 を返すため unexpected_pass となる。

## Parent Goal Ref

- Goal: unexpected_pass テスト用
- Desired Destination: unexpected_pass が検出されること

## Current Validated Scope

- テストフィクスチャのみ

## Remaining Parent Gaps

なし

## In Scope

- テストフィクスチャのみ

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `true` コマンドが常に exit 0 を返すこと（unexpected_pass テスト用）

## Verification Commands

```bash
# AC1
$ true
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: テストフィクスチャ用。

## Allowed Paths

- `.claude/skills/review-issue/tests/test_check_issue_contract.py`
"""


def run_contract_readiness(fixture_name: str, mode: str = "execute") -> tuple[dict, int]:
    """Run contract_readiness_check.py on a fixture file and return (parsed JSON, exit_code).

    Uses --body-file only (no --issue / gh / network) per AC7.
    shell=False is enforced by subprocess.run default per AC8.
    """
    fixture_path = FIXTURES_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

    result = subprocess.run(
        [sys.executable, str(CONTRACT_READINESS_SCRIPT_PATH), "--body-file", str(fixture_path), "--mode", mode],
        capture_output=True,
        text=True,
        shell=False,  # AC8: shell=True is NOT used
    )
    try:
        return json.loads(result.stdout), result.returncode
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"contract_readiness_check.py output is not valid JSON: {e}"
            f"\nstdout: {result.stdout}\nstderr: {result.stderr}"
        ) from e


def run_contract_readiness_from_content(content: str, mode: str = "execute") -> tuple[dict, int]:
    """Run contract_readiness_check.py with inline content via a temp file.

    Uses --body-file only (no --issue / gh / network) per AC7.
    shell=False is enforced by subprocess.run default per AC8.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    result = subprocess.run(
        [sys.executable, str(CONTRACT_READINESS_SCRIPT_PATH), "--body-file", tmp_path, "--mode", mode],
        capture_output=True,
        text=True,
        shell=False,  # AC8: shell=True is NOT used
    )
    Path(tmp_path).unlink(missing_ok=True)
    try:
        return json.loads(result.stdout), result.returncode
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"contract_readiness_check.py output is not valid JSON: {e}"
            f"\nstdout: {result.stdout}\nstderr: {result.stderr}"
        ) from e


class TestContractReadinessExecuteIntegration:
    """Integration tests for contract_readiness_check.py --mode execute via review-issue path.

    AC9: pytest contains integration fixture (not just mapper unit tests).
    AC3: unexpected_pass fixture → verdict: needs-fix + structured_blockers[].category == "unexpected_pass".
    """

    def test_execute_integration_mode_is_called(self):
        """GIVEN pass_issue.md fixture WHEN contract_readiness_check.py --mode execute runs
        THEN it returns ISSUE_CONTRACT_READINESS_RESULT_V1 schema (AC9: --mode execute actually called).

        Note: pass_issue.md has $ grep -r which may return exit 2 (file not found) → human_judgment.
        The key assertion is that the schema is ISSUE_CONTRACT_READINESS_RESULT_V1, confirming
        --mode execute was actually invoked (static mode would not call baseline_vc_preflight)."""
        result, exit_code = run_contract_readiness("pass_issue.md", mode="execute")
        assert result.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1", (
            f"Expected ISSUE_CONTRACT_READINESS_RESULT_V1 schema, got: {result.get('schema')}"
        )
        # --mode execute calls baseline_vc_preflight, which populates source_checks with
        # baseline_vc_preflight entry (absent in static mode).
        source_check_names = [s.get("name") for s in result.get("source_checks", [])]
        assert "baseline_vc_preflight" in source_check_names, (
            f"Expected 'baseline_vc_preflight' in source_checks (confirms --mode execute was called), "
            f"got: {source_check_names}"
        )

    def test_unexpected_pass_fixture_returns_needs_fix(self):
        """GIVEN inline unexpected_pass content (VC: $ true, always exit 0) WHEN --mode execute runs
        THEN status == needs_fix (AC3: verdict pathway produces needs-fix)."""
        result, exit_code = run_contract_readiness_from_content(_UNEXPECTED_PASS_ISSUE_CONTENT, mode="execute")
        assert result.get("status") == "needs_fix", (
            f"Expected needs_fix for unexpected_pass fixture, got: {result.get('status')}. "
            f"errors: {result.get('errors')}"
        )

    def test_unexpected_pass_category_in_errors(self):
        """GIVEN inline unexpected_pass content WHEN --mode execute runs
        THEN errors[] contains an entry with category == 'unexpected_pass' (AC3 full check)."""
        result, exit_code = run_contract_readiness_from_content(_UNEXPECTED_PASS_ISSUE_CONTENT, mode="execute")
        errors = result.get("errors", [])
        categories = [e.get("category") for e in errors]
        assert "unexpected_pass" in categories, (
            f"Expected 'unexpected_pass' category in errors, got: {categories}. "
            f"errors: {errors}"
        )

    def test_structured_blockers_lossless_passthrough(self):
        """GIVEN inline unexpected_pass content WHEN --mode execute runs
        THEN errors[] preserves source_check, source_payload fields (AC4: lossless pass-through)."""
        result, exit_code = run_contract_readiness_from_content(_UNEXPECTED_PASS_ISSUE_CONTENT, mode="execute")
        errors = result.get("errors", [])
        unexpected_pass_errors = [e for e in errors if e.get("category") == "unexpected_pass"]
        assert len(unexpected_pass_errors) > 0, (
            f"Expected at least one unexpected_pass error. errors: {errors}"
        )
        for err in unexpected_pass_errors:
            # AC4: source_check must be preserved
            assert "source_check" in err, f"error missing 'source_check': {err}"
            # AC4: source_payload with required sub-fields
            assert "source_payload" in err, f"error missing 'source_payload': {err}"
            payload = err["source_payload"]
            assert "decision" in payload, f"source_payload missing 'decision': {payload}"
            assert "classification" in payload, f"source_payload missing 'classification': {payload}"
            assert "exit_code" in payload, f"source_payload missing 'exit_code': {payload}"
            assert "command_hash" in payload, f"source_payload missing 'command_hash': {payload}"

    def test_human_judgment_not_collapsed_to_needs_fix(self):
        """GIVEN a fixture with a genuinely operator-only VC WHEN --mode execute runs
        THEN status == human_judgment (AC5: must NOT be collapsed to needs_fix).

        Uses _HUMAN_JUDGMENT_ISSUE_CONTENT which has VC:
        $ jq '.' fixture/e2e_check_issue_contract_human_judgment_unknown.json
        `jq` is on the preflight allowlist and is actually executed, but the referenced
        path does not exist, so its exit code (2) matches none of classify_result()'s
        known patterns and falls through to `category: unknown` -> human_judgment.

        Issue #2397 Scope Delta iteration 2: this fixture previously used
        `$ nonexistent-tool-xyz-abc`, an unallowlisted binary that
        `classify_result()` rejects *before* execution as `command_not_allowed`
        (a body-fixable state intentionally mapped to `needs_fix` by Issue #2397
        Scope Delta AC9, not human_judgment). The fixture was replaced with a
        genuinely operator-only (`unknown` classification) state so this test keeps
        verifying that human_judgment is not collapsed to needs_fix, without relying
        on the now-reclassified `command_not_allowed` category.
        The contract_readiness_check.py must NOT collapse this to needs_fix."""
        result, _exit_code = run_contract_readiness_from_content(_HUMAN_JUDGMENT_ISSUE_CONTENT, mode="execute")

        # The fixture must produce at least one error.
        # NOTE: `command_not_allowed` is kept in this tolerance list only for defensive
        # breadth; since Issue #2397 Scope Delta AC9, `command_not_allowed` itself maps
        # to `needs_fix` (not human_judgment), so it would not actually satisfy this
        # assertion on its own. This fixture exercises `unknown` instead (jq against a
        # nonexistent path, exit_code=2, decision=human_judgment), which remains
        # genuinely operator-only and unaffected by the AC9 change.
        errors = result.get("errors", [])
        non_needs_fix_errors = [
            e for e in errors
            if e.get("category") in ("env_missing_dep", "timeout", "unknown", "command_not_allowed")
            or e.get("source_payload", {}).get("decision") in ("human_judgment", "blocked")
        ]
        assert len(non_needs_fix_errors) > 0, (
            f"human_judgment/blocked fixture should produce at least one non-needs_fix error "
            f"(env_missing_dep/timeout/unknown/command_not_allowed). errors: {errors}"
        )

        # Overall status must be human_judgment or blocked (not needs_fix) (AC5)
        # unknown-classification blocked VCs (e.g. jq against a nonexistent path) map to
        # status=human_judgment at the contract level.
        assert result.get("status") in ("human_judgment", "blocked"), (
            f"non-needs_fix errors must NOT collapse overall status to needs_fix. "
            f"status={result.get('status')}, non_needs_fix_errors={non_needs_fix_errors}"
        )

        # Confirm: no error has been re-classified as needs_fix in source_payload
        for err in non_needs_fix_errors:
            payload = err.get("source_payload", {})
            assert payload.get("classification") != "needs_fix", (
                f"blocked/human_judgment error must not be reclassified as needs_fix in source_payload: {err}"
            )

    def test_execute_uses_body_file_only_no_network(self):
        """GIVEN a fixture file WHEN --mode execute runs with --body-file
        THEN it succeeds without requiring gh auth or network access (AC7).

        This is verified by running with --body-file only (no --issue / --repo).
        If gh were required, it would fail in offline / unauthed environments."""
        result, exit_code = run_contract_readiness("pass_issue.md", mode="execute")
        # Verify we got a valid ISSUE_CONTRACT_READINESS_RESULT_V1 response (not an auth error)
        assert result.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1", (
            f"Expected ISSUE_CONTRACT_READINESS_RESULT_V1, got: {result.get('schema')}"
        )
        # Input error (gh auth / network) would set status=human_judgment with INPUT001 rule_id
        errors = result.get("errors", [])
        input_errors = [e for e in errors if e.get("rule_id") == "INPUT001"]
        assert len(input_errors) == 0, (
            f"INPUT001 (network/auth) errors should not appear when using --body-file: {input_errors}"
        )

    def test_shell_false_enforced(self):
        """GIVEN contract_readiness_check.py subprocess call WHEN it runs
        THEN shell=False is used (AC8: shell=True must NOT be introduced).

        Verifies by inspecting the script source for shell=True patterns."""
        script_content = CONTRACT_READINESS_SCRIPT_PATH.read_text(encoding="utf-8")
        assert "shell=True" not in script_content, (
            "contract_readiness_check.py must not use shell=True. "
            "Existing shell=False convention must be preserved."
        )


class TestJsonStdoutPurity:
    """GIVEN --json flag WHEN checker runs THEN stdout contains only valid JSON (AC1/AC3: #574)."""

    def _run_raw(self, fixture_name: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        """Run checker with --json and return raw CompletedProcess (stdout/stderr unmodified)."""
        import os
        fixture_path = FIXTURES_DIR / fixture_name
        assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--file", str(fixture_path), "--json"],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_stdout_is_valid_json_only(self):
        """GIVEN pass_issue.md with --json flag WHEN checker runs
        THEN stdout is exactly one valid JSON object with no extra bytes."""
        result = self._run_raw("pass_issue.md")
        assert result.returncode in (0, 1), (
            f"Unexpected exit code {result.returncode}. stderr: {result.stderr}"
        )
        # stdout must parse as JSON without any leading/trailing non-JSON text
        stripped = result.stdout.strip()
        assert stripped, "stdout must not be empty in --json mode"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"stdout is not valid JSON: {e}\nstdout repr: {repr(result.stdout[:500])}"
            ) from e
        assert isinstance(parsed, dict), f"JSON output must be a dict, got {type(parsed)}"
        assert "verdict" in parsed, "JSON output must contain 'verdict' field"

    def test_stdout_contains_no_warning_text(self):
        """GIVEN pass_issue.md with --json flag WHEN checker runs
        THEN stdout contains no DeprecationWarning or warning text."""
        result = self._run_raw("pass_issue.md")
        assert "DeprecationWarning" not in result.stdout, (
            f"stdout must not contain DeprecationWarning. stdout: {repr(result.stdout[:500])}"
        )
        assert "Warning" not in result.stdout, (
            f"stdout must not contain Warning text. stdout: {repr(result.stdout[:500])}"
        )

    def test_no_deprecation_warning_with_pythonwarnings_error(self):
        """GIVEN PYTHONWARNINGS=error::DeprecationWarning WHEN --json checker runs
        THEN exit code is 0 or 1 (not 2/crash from DeprecationWarning treated as error)."""
        result = self._run_raw(
            "pass_issue.md",
            extra_env={"PYTHONWARNINGS": "error::DeprecationWarning"},
        )
        assert result.returncode in (0, 1), (
            f"DeprecationWarning was raised as an error (exit code {result.returncode}). "
            f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )
        # stdout must still be valid JSON
        stripped = result.stdout.strip()
        assert stripped, "stdout must not be empty even with PYTHONWARNINGS=error"
        parsed = json.loads(stripped)
        assert "verdict" in parsed

    def test_utcnow_replaced_in_source(self):
        """GIVEN check_issue_contract.py source WHEN inspected
        THEN datetime.datetime.utcnow() is not present (AC2: #574)."""
        script_content = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "datetime.utcnow()" not in script_content, (
            "datetime.datetime.utcnow() must be replaced with "
            "datetime.datetime.now(datetime.timezone.utc) — see Issue #574 AC2."
        )

    def test_generated_at_field_is_present_in_json_output(self):
        """GIVEN pass_issue.md with --json flag WHEN checker runs
        THEN output JSON contains 'generated_at' field with UTC timestamp."""
        result = self._run_raw(
            "pass_issue.md",
            extra_env={"PYTHONWARNINGS": "error::DeprecationWarning"},
        )
        assert result.returncode in (0, 1)
        parsed = json.loads(result.stdout.strip())
        assert "generated_at" in parsed, "JSON output must contain 'generated_at' field"
        # Verify it looks like a UTC timestamp (ends with Z)
        generated_at = parsed["generated_at"]
        assert generated_at.endswith("Z"), (
            f"generated_at must end with 'Z' (UTC), got: {generated_at}"
        )


# ---------------------------------------------------------------------------
# AC3 / AC3b: C5 grouped AC comment support (Issue #814)
# ---------------------------------------------------------------------------

# Import the check function directly for unit-level testing
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from check_issue_contract import check_c5_ac_vc_alignment, _extract_vc_ac_refs  # noqa: E402


class TestC5GroupedAcComment:
    """AC3: C5 parser recognises grouped AC comments (# AC2, AC3, AC4).
    AC3b: range notation (# AC2-AC4) is NOT supported.
    """

    def _body_with(self, ac_section: str, vc_section: str) -> str:
        return (
            "## Acceptance Criteria\n\n"
            + ac_section
            + "\n\n## Verification Commands\n\n"
            + "```bash\n"
            + vc_section
            + "\n```\n"
        )

    def test_single_ac_reference_still_works(self):
        """Single # AC1 reference is recognised (regression)."""
        body = self._body_with(
            "- [ ] AC1: foo\n",
            "# AC1\n$ uv run pytest tests/ -q\n",
        )
        result, issues = check_c5_ac_vc_alignment(body)
        assert result == "pass", f"Expected pass, got {result}. issues: {issues}"

    def test_grouped_ac_comment_all_recognised(self):
        """AC3: # AC2, AC3, AC4 grouped comment -> all three numbers extracted as referenced."""
        body = self._body_with(
            "- [ ] AC2: bar\n- [ ] AC3: baz\n- [ ] AC4: qux\n",
            "# AC2, AC3, AC4\n$ uv run pytest tests/ -q\n",
        )
        result, issues = check_c5_ac_vc_alignment(body)
        assert result == "pass", f"Expected pass, got {result}. issues: {issues}"

    def test_grouped_ac_comment_partial_coverage(self):
        """Grouped comment covering only some ACs -> fail for the uncovered ones."""
        body = self._body_with(
            "- [ ] AC1: first\n- [ ] AC2: second\n- [ ] AC3: third\n",
            "# AC1, AC2\n$ uv run pytest tests/ -q\n",
        )
        result, issues = check_c5_ac_vc_alignment(body)
        assert result == "fail", f"Expected fail (AC3 uncovered), got {result}"
        assert any("AC3" in msg for msg in issues), f"Expected AC3 in issues: {issues}"

    def test_c5_research_no_ac_numbers_is_na(self):
        """#2480: research issue_kind with unnumbered AC checkboxes -> NA, not FAIL.

        .github/ISSUE_TEMPLATE/research.yml provides unnumbered '- [ ]' AC
        checkboxes; C5 must not FAIL a research Issue merely for following
        the official template.
        """
        body = self._body_with(
            "- [ ] 比較結果が本 Issue 本文に記載されている\n"
            "- [ ] 本 Issue は close 可能、もしくは追加調査要件が明示されている\n",
            "$ echo check\n",
        )
        result, issues = check_c5_ac_vc_alignment(body, issue_kind="research")
        assert result == "n/a", f"Expected n/a, got {result}. issues: {issues}"
        assert issues == []

    def test_c5_tracking_no_ac_numbers_is_na(self):
        """#2480: tracking issue_kind gets the same unnumbered-AC NA treatment as research."""
        body = self._body_with(
            "- [ ] 追跡対象の状態が記載されている\n",
            "$ echo check\n",
        )
        result, issues = check_c5_ac_vc_alignment(body, issue_kind="tracking")
        assert result == "n/a", f"Expected n/a, got {result}. issues: {issues}"
        assert issues == []

    def test_c5_implementation_no_ac_numbers_still_fail(self):
        """#2480: default/implementation issue_kind keeps the strict unconditional FAIL."""
        body = self._body_with(
            "- [ ] 番号のない AC\n",
            "$ echo check\n",
        )
        result, issues = check_c5_ac_vc_alignment(body)
        assert result == "fail", f"Expected fail, got {result}. issues: {issues}"
        assert any("AC[N]" in msg for msg in issues), f"Expected AC[N] message: {issues}"

        result_explicit, _ = check_c5_ac_vc_alignment(body, issue_kind="implementation")
        assert result_explicit == "fail"

    def test_c5_research_with_ac_numbers_alignment_still_applies(self):
        """#2480: research issue_kind with numbered AC keeps normal alignment enforcement."""
        body = self._body_with(
            "- [ ] AC1: foo\n- [ ] AC2: bar\n",
            "# AC1\n$ echo check\n",
        )
        result, issues = check_c5_ac_vc_alignment(body, issue_kind="research")
        assert result == "fail", f"Expected fail (AC2 uncovered), got {result}. issues: {issues}"
        assert any("AC2" in msg for msg in issues), f"Expected AC2 in issues: {issues}"

    def test_extract_vc_ac_refs_single(self):
        """_extract_vc_ac_refs returns single number correctly."""
        refs = _extract_vc_ac_refs("# AC1\n$ foo")
        assert refs == {"1"}

    def test_extract_vc_ac_refs_grouped(self):
        """_extract_vc_ac_refs returns all numbers from grouped comment."""
        refs = _extract_vc_ac_refs("# AC2, AC3, AC4\n$ foo")
        assert refs == {"2", "3", "4"}

    def test_extract_vc_ac_refs_multiple_lines(self):
        """_extract_vc_ac_refs handles multiple comment lines."""
        vc = "# AC1\n$ cmd1\n# AC2, AC3\n$ cmd2\n"
        refs = _extract_vc_ac_refs(vc)
        assert refs == {"1", "2", "3"}

    def test_range_notation_not_supported(self):
        """AC3b: range notation # AC2-AC4 is NOT treated as grouped (not in scope).

        Range '# AC2-AC4' only extracts AC2 (the first number matched),
        because '-AC4' is not the comma pattern. The '-' is not a separator.
        This is the expected non-support behaviour per AC3b.
        """
        # '# AC2-AC4' should NOT yield {"2","3","4"}
        refs = _extract_vc_ac_refs("# AC2-AC4\n$ foo")
        # Only AC2 is captured; AC3 and AC4 are NOT (range not supported)
        assert "3" not in refs, (
            "Range notation # AC2-AC4 should NOT yield AC3 (range not supported per AC3b)"
        )
        assert "4" not in refs, (
            "Range notation # AC2-AC4 should NOT yield AC4 (range not supported per AC3b)"
        )

    def test_grouped_with_spaces_around_comma(self):
        """Grouped AC refs with varying whitespace are parsed correctly."""
        refs = _extract_vc_ac_refs("# AC1 , AC2 , AC3\n$ foo")
        assert "1" in refs
        assert "2" in refs
        assert "3" in refs

    def test_prose_hash_ac_not_counted(self):
        """B1: prose comment '説明: # AC2 は未検証' must NOT be extracted as C5 ref."""
        refs = _extract_vc_ac_refs("説明: # AC2 は未検証\n")
        assert refs == set()

    def test_echo_hash_ac_not_counted(self):
        """B1: shell echo containing '# AC2' must NOT be extracted as C5 ref."""
        refs = _extract_vc_ac_refs('$ echo "# AC2"\n')
        assert refs == set()

    def test_url_fragment_ac_not_counted(self):
        """B1: URL fragment '#AC2' must NOT be extracted as C5 ref."""
        refs = _extract_vc_ac_refs("$ curl https://example.test/#AC2\n")
        assert refs == set()

    def test_filename_hash_ac_not_counted(self):
        """B1: filename containing '#AC2' must NOT be extracted as C5 ref."""
        refs = _extract_vc_ac_refs("$ cat docs/#AC2.md\n")
        assert refs == set()


class TestProductSpecCheckerComposition:
    def test_product_spec_checker_uses_same_body_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """review-issue must pass the same body-file path to product-spec checker."""
        fixture_body = """## Machine-Readable Contract
```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Outcome

No docs/product scope.

## Acceptance Criteria

- [ ] AC1: checker wiring is exercised

## Verification Commands

```bash
# AC1
$ test -f src/example.ts
```

## Stop Conditions

- 1
- 2
- 3
- 4
- 5
- 6

## Runtime Verification Applicability

decision: not_applicable
reason: test fixture only.

## Allowed Paths

- `src/example.ts`

"""
        fixture_path = tmp_path / "issue.md"
        fixture_path.write_text(fixture_body, encoding="utf-8")
        expected_sha = checker._sha256_prefixed(fixture_body)
        seen_paths: list[str] = []

        def fake_run(cmd, capture_output, text, timeout):
            seen_paths.append(cmd[cmd.index("--body-file") + 1])
            payload = {
                "schema": "product_spec_check/v1",
                "applicability": "not_applicable",
                "decision": "pass",
                "blocked_reasons": [],
                "body_sha256": expected_sha,
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

        monkeypatch.setattr(checker.subprocess, "run", fake_run)

        result = checker.run_checks(
            fixture_body,
            body_file_path=str(fixture_path),
        )

        assert seen_paths == [str(fixture_path)]
        assert not any(
            "product spec checker" in issue for issue in result.blocking_issues
        )

    def test_product_spec_checker_hash_mismatch_blocks_review(self, monkeypatch: pytest.MonkeyPatch):
        """hash mismatch between review-issue body and checker output must fail closed."""
        body = """## Machine-Readable Contract
```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Outcome

No docs/product scope.
"""

        monkeypatch.setattr(
            checker,
            "_run_product_spec_check",
            lambda *_args, **_kwargs: (
                {
                    "schema": "product_spec_check/v1",
                    "applicability": "not_applicable",
                    "decision": "pass",
                    "triggers": {},
                    "conditions": {},
                    "blocked_reasons": [],
                    "body_sha256": "sha256:mismatch",
                    "source_provenance": {
                        "source_type": "github_issue_body",
                        "body_file": None,
                    },
                },
                0,
                None,
            ),
        )

        result = checker.run_checks(body)

        assert result.verdict == "needs-fix"
        assert any(blocker["code"] == "PRODUCT_SPEC" for blocker in result.structured_blockers)

    def test_product_spec_checker_invalid_pair_blocks_review(self, monkeypatch: pytest.MonkeyPatch):
        """not_applicable + human_judgment must fail closed via shared pair invariant evaluator."""
        body = """## Machine-Readable Contract
```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Outcome

No docs/product scope.
"""
        body_sha = checker._sha256_prefixed(body)

        monkeypatch.setattr(
            checker,
            "_run_product_spec_check",
            lambda *_args, **_kwargs: (
                {
                    "schema": "product_spec_check/v1",
                    "applicability": "not_applicable",
                    "decision": "human_judgment",
                    "triggers": {},
                    "conditions": {},
                    "blocked_reasons": [],
                    "body_sha256": body_sha,
                    "source_provenance": {
                        "source_type": "github_issue_body",
                        "body_file": None,
                    },
                },
                0,
                None,
            ),
        )

        result = checker.run_checks(body)

        assert result.verdict == "needs-fix"
        assert any(
            "not_applicable requires decision=pass" in issue
            for issue in result.blocking_issues
        )


class TestFetchIssueBodyRawParity:
    """PR #1342 review fix: fetch_issue_body() は gh --json の body field を
    そのまま返し、raw body hash (contract_readiness_check.py 等が使う
    canonical hash) と一致すること（trailing newline を .strip() で
    削除しない）。"""

    def test_fetch_issue_body_preserves_raw_body_trailing_newline(self, monkeypatch):
        import hashlib

        body = "line1\nline2\n"
        payload = {
            "title": "T",
            "body": body,
            "labels": [{"name": "enhancement"}, {"name": "phase/implementation"}],
        }

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(payload, ensure_ascii=False) + "\n",
                stderr="",
            )

        monkeypatch.setattr(checker.subprocess, "run", fake_run)

        fetched_body, labels, title = checker.fetch_issue_body(
            1261,
            "example-org/example-repo",
        )

        assert fetched_body == body
        assert labels == "enhancement,phase/implementation"
        assert title == "T"
        assert checker._sha256_prefixed(fetched_body) == (
            "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        )

    def test_fetch_issue_body_rejects_non_object_json(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps([1, 2, 3]) + "\n",
                stderr="",
            )

        monkeypatch.setattr(checker.subprocess, "run", fake_run)

        with pytest.raises(SystemExit) as exc_info:
            checker.fetch_issue_body(1261, "example-org/example-repo")
        assert exc_info.value.code == 2
