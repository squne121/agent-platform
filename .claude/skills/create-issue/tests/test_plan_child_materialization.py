"""Tests for plan_child_materialization.py.

Uses #254-equivalent fixtures to verify that C254 child lines are classified
into missing / existing_open / existing_closed / stale_body_only (and ambiguous)
correctly.

GIVEN a delivery-rollup parent issue body
WHEN plan_child_materialization.build_plan() is called
THEN each child entry receives the correct status classification.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


# Resolve the scripts directory so we can import without installing.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import plan_child_materialization as pmc  # noqa: E402

_GUARDS_DIR = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards"
sys.path.insert(0, str(_GUARDS_DIR))
import repo_identity  # noqa: E402

TRUSTED_REPO = repo_identity.load_trusted_repo_slug()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Simulated #254 parent issue body with three child types:
#:  - C254-1: has a matching open issue #281 without placeholder → existing_open
#:  - C254-3: has placeholder （未起票） with no issue ref → missing
#:  - C254-5: has both placeholder AND a real open issue ref #285 → stale_body_only
FIXTURE_PARENT_BODY_254 = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Outcome

#254 の child issues が全て close されたとき、本 Issue を close する。

## Child Issues

- C254-1 docs: docs/product/game-thesis.md を追加 #281
- C254-3 docs: docs/dev/runtime-verification-policy.md を追加（未起票）
- C254-5 feat: SDD 採否 ADR を追加（未起票） #285

## Background

Some background text that mentions C254-9 and #999 but should NOT be parsed.
"""


#: #244-style fixture with ### subheading and A/B/C/D track IDs
FIXTURE_PARENT_BODY_244_ABCD = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Outcome

#244 の child issues が全て close されたとき、本 Issue を close する。

## Child Issues

### 実装トラック

- [ ] A: Issue body validator 実装 — #327
- [ ] B: baseline_vc_preflight.py 実装 — #329
- [ ] C: validate_pr_body.py 実装 — #330
- [ ] D: text/markdown lint 導入 — #331

## Background

Some background.
"""


def _make_issue_info(number: int, state: str = "OPEN", state_reason=None) -> pmc.ExistingIssueInfo:
    return pmc.ExistingIssueInfo(
        number=number,
        state=state,
        state_reason=state_reason,
        url=f"https://github.com/{TRUSTED_REPO}/issues/{number}",
    )


def _build_dry_run(body: str, parent_issue: int = 254, repo: str = "") -> pmc.Plan:
    """Build a plan in dry-run mode (no GitHub API calls)."""
    return pmc.build_plan(body, parent_issue=parent_issue, repo=repo, dry_run=True)


def _build_live(
    body: str,
    parent_issue: int = 254,
    repo: str = TRUSTED_REPO,
    view_side_effects: dict | None = None,
    search_return: list | None = None,
    subissues_actual: list | None = None,
    subissues_readback_status: str = "ok",
) -> pmc.Plan:
    """Build a plan in live mode with mocked gh calls.

    view_side_effects: {issue_number: ExistingIssueInfo | None}
        If None, the API call "failed" for that issue.
    search_return: list of candidate dicts returned by _search_dedupe_candidates.
    subissues_actual: list of sub-issue dicts (items) for SubissuesReadback.
    subissues_readback_status: status for SubissuesReadback (default "ok").
    """
    if view_side_effects is None:
        view_side_effects = {}
    if search_return is None:
        search_return = []
    if subissues_actual is None:
        subissues_actual = []

    # Build SubissuesReadback as _fetch_subissues_actual now returns
    readback = pmc.SubissuesReadback(
        status=subissues_readback_status,  # type: ignore[arg-type]
        items=subissues_actual,
        complete=(subissues_readback_status == "ok"),
    )

    def fake_view(r, n, g="gh"):
        return view_side_effects.get(n)

    with patch.object(pmc, "_view_issue", side_effect=fake_view), \
         patch.object(pmc, "_search_dedupe_candidates", return_value=search_return), \
         patch.object(pmc, "_fetch_subissues_actual", return_value=readback):
        return pmc.build_plan(body, parent_issue=parent_issue, repo=repo, dry_run=False)


def _child_by_id(plan: pmc.Plan, child_id: str) -> pmc.ChildEntry:
    for child in plan.children:
        if child.child_id == child_id:
            return child
    raise KeyError(f"child_id={child_id!r} not found in plan")


# ---------------------------------------------------------------------------
# Tests: parent_mode extraction
# ---------------------------------------------------------------------------


class TestParentModeExtraction:
    def test_extracts_delivery_rollup_from_body(self) -> None:
        """GIVEN a body with parent_mode: delivery-rollup
        WHEN _extract_parent_mode is called
        THEN it returns 'delivery-rollup'.
        """
        body = "```yaml\nparent_mode: delivery-rollup\n```"
        assert pmc._extract_parent_mode(body) == "delivery-rollup"

    def test_returns_unknown_when_absent(self) -> None:
        """GIVEN a body without parent_mode
        WHEN _extract_parent_mode is called
        THEN it returns 'unknown' (not 'delivery-rollup').
        """
        body = "No machine-readable contract here"
        assert pmc._extract_parent_mode(body) == "unknown"

    def test_parent_mode_unknown_produces_human_escalation(self) -> None:
        """GIVEN a body without parent_mode (broken Machine-Readable Contract)
        WHEN build_plan is called
        THEN children that are parsed get action=human_escalation.
        """
        body = """\
## Child Issues

- C254-3 docs: something（未起票）
"""
        plan = _build_dry_run(body, parent_issue=254)
        # Should have parsed the child
        assert len(plan.children) > 0
        child = _child_by_id(plan, "C254-3")
        assert child.action == "human_escalation"


# ---------------------------------------------------------------------------
# Tests: closure_mode extraction
# ---------------------------------------------------------------------------


class TestClosureModeExtraction:
    def test_extracts_child_complete(self) -> None:
        """GIVEN a body with closure_mode: child-complete
        WHEN _extract_closure_mode is called
        THEN it returns 'child-complete'.
        """
        body = "```yaml\nclosure_mode: child-complete\n```"
        assert pmc._extract_closure_mode(body) == "child-complete"

    def test_returns_unknown_when_absent(self) -> None:
        """GIVEN a body without closure_mode
        WHEN _extract_closure_mode is called
        THEN it returns 'unknown'.
        """
        body = "No machine-readable contract here"
        assert pmc._extract_closure_mode(body) == "unknown"


# ---------------------------------------------------------------------------
# Tests: section scoping — ## Child Issues boundary
# ---------------------------------------------------------------------------


class TestChildIssueSectionScoping:
    def test_only_parses_child_issues_section(self) -> None:
        """GIVEN a body with Cxxx-N patterns outside ## Child Issues
        WHEN _parse_child_lines is called
        THEN only lines from ## Child Issues section are returned.
        """
        body = """\
## Background

Some text mentions C254-9 for context.

## Child Issues

- C254-3 docs: something（未起票）

## Remaining Parent Gaps

- C254-7 is referenced here but NOT a child line
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "C254-3" in child_ids
        assert "C254-9" not in child_ids
        assert "C254-7" not in child_ids

    def test_section_stops_at_next_heading(self) -> None:
        """GIVEN a ## Child Issues section followed by another heading
        WHEN _parse_child_lines is called
        THEN lines after the next heading are not parsed.
        """
        body = """\
## Child Issues

- C254-1 docs: something #281

## Verification Commands

- C254-99 should not be parsed
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "C254-1" in child_ids
        assert "C254-99" not in child_ids

    def test_no_child_issues_section_returns_empty(self) -> None:
        """GIVEN a body without ## Child Issues heading
        WHEN _parse_child_lines is called
        THEN results is empty.
        """
        body = """\
## Outcome

- C254-3 is mentioned here but there's no Child Issues section
"""
        results = pmc._parse_child_lines(body)
        assert results == []

    def test_section_includes_subheading_children(self) -> None:
        """GIVEN a ## Child Issues section with a ### subheading
        WHEN _parse_child_lines is called
        THEN children under the subheading are also returned (not stopped by ###).

        Regression: previously the parser stopped at ### subheadings, missing
        A/B/C/D tracks in '### 実装トラック' style bodies (#244 pattern).
        """
        body = """\
## Child Issues

### 実装トラック

- [ ] A: Issue body validator — #327
- [ ] B: baseline_vc_preflight.py — #329

## Background
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "A" in child_ids
        assert "B" in child_ids


# ---------------------------------------------------------------------------
# Tests: AC1 — A/B/C/D format and C254-N format parsing
# ---------------------------------------------------------------------------


class TestABCDTrackIdParsing:
    """AC1: plan_child_materialization.py can parse both C254-3 format and A:/B:/C:/D: format."""

    def test_abcd_track_ids_parsed_in_child_issues_section(self) -> None:
        """GIVEN A:/B:/C:/D: format lines under ## Child Issues
        WHEN _parse_child_lines is called
        THEN all four track IDs are detected.

        AC1: abcd format support.
        """
        body = """\
## Child Issues

- [ ] A: Issue body validator 実装 — #327
- [ ] B: baseline_vc_preflight.py 実装 — #329
- [ ] C: validate_pr_body.py 実装 — #330
- [ ] D: text/markdown lint 導入 — #331
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "A" in child_ids, "A track should be parsed"
        assert "B" in child_ids, "B track should be parsed"
        assert "C" in child_ids, "C track should be parsed"
        assert "D" in child_ids, "D track should be parsed"
        assert len(results) == 4

    def test_c254_format_still_parsed(self) -> None:
        """GIVEN C254-N format lines under ## Child Issues
        WHEN _parse_child_lines is called
        THEN C254-N entries are still detected.

        AC1: c254 format not regressed.
        """
        body = """\
## Child Issues

- C254-1 docs: something #281
- C254-3 docs: something（未起票）
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "C254-1" in child_ids
        assert "C254-3" in child_ids

    def test_abcd_with_subheading_fixture_244(self) -> None:
        """GIVEN #244-style body with ### 実装トラック subheading
        WHEN build_plan is called
        THEN all 4 A/B/C/D tracks are detected.

        AC1: subheading traversal for abcd.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_244_ABCD, parent_issue=244)
        child_ids = [c.child_id for c in plan.children]
        assert "A" in child_ids
        assert "B" in child_ids
        assert "C" in child_ids
        assert "D" in child_ids
        assert len(plan.children) == 4

    def test_abcd_dedupe_key_format(self) -> None:
        """GIVEN A/B/C/D track IDs under delivery-rollup parent
        WHEN build_plan is called
        THEN dedupe_key is 'delivery-rollup:<parent>:<child_id>'.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_244_ABCD, parent_issue=244)
        child_a = _child_by_id(plan, "A")
        assert child_a.dedupe_key == "delivery-rollup:244:A"


# ---------------------------------------------------------------------------
# Tests: AC2a, AC2b — candidate inventory and parser_gap_report
# ---------------------------------------------------------------------------


class TestCandidateInventory:
    """AC2a: candidate inventory before parsing."""

    def test_body_inventory_present_in_plan(self) -> None:
        """GIVEN a body with ## Child Issues
        WHEN build_plan is called
        THEN plan.body_inventory is not None.

        AC2a: candidate_inventory field present.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.body_inventory is not None

    def test_candidate_count_equals_parsed_count_for_clean_body(self) -> None:
        """GIVEN a clean body where all candidate lines parse successfully
        WHEN build_plan is called
        THEN candidate_count == parsed_count.

        AC2a: candidate_inventory.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.candidate_count == bi.parsed_count, (
            f"Expected candidate_count == parsed_count, got "
            f"candidate={bi.candidate_count}, parsed={bi.parsed_count}"
        )

    def test_candidate_count_for_244_fixture(self) -> None:
        """GIVEN #244-style body with 4 A/B/C/D tracks
        WHEN build_plan is called
        THEN candidate_count == 4 and parsed_count == 4.

        AC2a: candidate_inventory for abcd.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_244_ABCD, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.candidate_count == 4, f"Expected 4 candidates, got {bi.candidate_count}"
        assert bi.parsed_count == 4, f"Expected 4 parsed, got {bi.parsed_count}"

    def test_body_inventory_section_not_found(self) -> None:
        """GIVEN a body without ## Child Issues
        WHEN build_plan is called
        THEN body_inventory.child_issues_section_found is False.

        AC2a: candidate_inventory when section absent.
        """
        body = "## Outcome\n\nNo children.\n"
        plan = _build_dry_run(body)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.child_issues_section_found is False
        assert bi.candidate_count == 0
        assert bi.parsed_count == 0


class TestParserGapReport:
    """AC2b: parser_gap_report schema."""

    def test_parser_gap_report_is_empty_for_clean_body(self) -> None:
        """GIVEN a clean body where all candidates parse
        WHEN build_plan is called
        THEN parser_gap_report is empty.

        AC2b: parser_gap_report.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.parser_gap_report == []

    def test_parser_gap_report_schema_fields(self) -> None:
        """GIVEN a body with a gap entry (from duplicate_child_id or malformed line)
        WHEN build_plan is called and gap report produced
        THEN gap items have required schema fields.

        AC2b: gap schema (line_number, raw_line, gap_reason, suggested_repair,
              repair_confidence, minimal_context).
        """
        # Create a body with a duplicate child_id to trigger gap report
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- [ ] A: valid entry — #327
- [ ] A: duplicate A — #400
"""
        plan = _build_dry_run(body, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        # Parser_gap_report should have duplicate_child_id gap
        assert isinstance(bi.parser_gap_report, list)
        if bi.parser_gap_report:
            gap = bi.parser_gap_report[0]
            assert hasattr(gap, "line_number"), "gap must have line_number"
            assert hasattr(gap, "raw_line"), "gap must have raw_line"
            assert hasattr(gap, "gap_reason"), "gap must have gap_reason"
            assert hasattr(gap, "suggested_repair"), "gap must have suggested_repair"
            assert hasattr(gap, "repair_confidence"), "gap must have repair_confidence"
            assert hasattr(gap, "minimal_context"), "gap must have minimal_context"

    def test_parser_gap_report_yaml_output(self) -> None:
        """GIVEN any plan
        WHEN plan_to_yaml is called
        THEN output contains parser_gap_report section.

        AC2b: yaml serialization of parser_gap_report.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "parser_gap_report:" in yaml_output
        assert "candidate_count:" in yaml_output
        assert "parsed_count:" in yaml_output


# ---------------------------------------------------------------------------
# Tests: AC2c — repairable parser gap → issue-author delegation
# ---------------------------------------------------------------------------


class TestRepairableParserGap:
    """AC2c: repairable_parser_gap / repair_confidence routing."""

    def test_unsupported_child_id_format_is_repairable(self) -> None:
        """GIVEN a line with 'A issue title' (space not colon) format
        WHEN _classify_parser_gap is called
        THEN gap_reason is unsupported_child_id_format and repair_confidence is high.

        AC2c: repairable_parser_gap.
        """
        gap = pmc._classify_parser_gap(
            line_number=5,
            raw_line="- [ ] A Issue body validator",
            stripped="- [ ] A Issue body validator",
            parent_issue=244,
        )
        assert gap is not None
        assert gap.gap_reason == "unsupported_child_id_format"
        assert gap.repair_confidence == "high"
        assert gap.suggested_repair is not None

    def test_unsupported_child_id_format_suggested_repair(self) -> None:
        """GIVEN a repairable line
        WHEN _classify_parser_gap is called
        THEN suggested_repair contains corrected form.

        AC2c: repair_confidence high → suggested_repair available.
        """
        gap = pmc._classify_parser_gap(
            line_number=5,
            raw_line="- [ ] B baseline_vc_preflight.py 実装",
            stripped="- [ ] B baseline_vc_preflight.py 実装",
            parent_issue=244,
        )
        assert gap is not None
        assert gap.repair_confidence == "high"
        # suggested_repair should contain the corrected colon-separated form
        assert gap.suggested_repair is not None
        assert "B:" in gap.suggested_repair

    def test_repair_confidence_high_in_classifier(self) -> None:
        """GIVEN a repairable gap
        WHEN _classify_parser_gap returns
        THEN repair_confidence is 'high'.

        AC2c: repair_confidence field.
        """
        gap = pmc._classify_parser_gap(
            line_number=5,
            raw_line="- [ ] C validate_pr_body.py 実装",
            stripped="- [ ] C validate_pr_body.py 実装",
            parent_issue=244,
        )
        assert gap is not None
        assert gap.repair_confidence == "high"


# ---------------------------------------------------------------------------
# Tests: AC2e — unsafe parser gaps → human_escalation
# ---------------------------------------------------------------------------


class TestUnsafeParserGap:
    """AC2e: unsafe_parser_gap scenarios that require human_escalation."""

    def test_duplicate_child_id_produces_low_confidence_gap(self) -> None:
        """GIVEN a body with duplicate child_id 'A' appearing twice
        WHEN build_plan is called
        THEN parser_gap_report contains gap with gap_reason duplicate_child_id.

        AC2e: duplicate_child_id → human_escalation.
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- [ ] A: Issue body validator — #327
- [ ] A: duplicate A track — #400
"""
        plan = _build_dry_run(body, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        gap_reasons = [g.gap_reason for g in bi.parser_gap_report]
        assert "duplicate_child_id" in gap_reasons, (
            f"Expected duplicate_child_id in gap_reasons, got: {gap_reasons}"
        )

    def test_multiple_issue_refs_produces_low_confidence_gap(self) -> None:
        """GIVEN a line with multiple issue refs
        WHEN _classify_parser_gap is called
        THEN gap has multiple_issue_refs reason and low confidence.

        AC2e: multiple_issue_refs → human_escalation.
        """
        gap = pmc._classify_parser_gap(
            line_number=5,
            raw_line="- [ ] A: Issue body validator #327 #400",
            stripped="- [ ] A: Issue body validator #327 #400",
            parent_issue=244,
        )
        assert gap is not None
        assert gap.gap_reason == "multiple_issue_refs"
        assert gap.repair_confidence == "low"
        assert gap.suggested_repair is None

    def test_missing_title_produces_low_confidence_gap(self) -> None:
        """GIVEN a line with only a child_id but no title
        WHEN _classify_parser_gap is called
        THEN gap has missing_title reason and low confidence.

        AC2e: missing_title → human_escalation.
        """
        gap = pmc._classify_parser_gap(
            line_number=5,
            raw_line="- [ ] A:",
            stripped="- [ ] A:",
            parent_issue=244,
        )
        assert gap is not None
        assert gap.gap_reason == "missing_title"
        assert gap.repair_confidence == "low"
        assert gap.suggested_repair is None


# ---------------------------------------------------------------------------
# Tests: AC3 — body_inventory / github_subissues_actual separation
# ---------------------------------------------------------------------------


class TestInventorySplit:
    """AC3: body_inventory (desired) / github_subissues_actual (actual) are separate."""

    def test_inventory_split_fields_present(self) -> None:
        """GIVEN a plan
        WHEN plan is built
        THEN plan has both body_inventory and github_subissues_actual fields.

        AC3: inventory_split.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert hasattr(plan, "body_inventory"), "plan must have body_inventory"
        assert hasattr(plan, "github_subissues_actual"), "plan must have github_subissues_actual"

    def test_subissues_actual_none_in_dry_run(self) -> None:
        """GIVEN dry-run mode (no GitHub API)
        WHEN build_plan is called
        THEN github_subissues_actual is None (no API call made in dry-run).

        AC3: subissues_actual is None when not fetched (dry-run).
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.github_subissues_actual is None

    def test_subissues_actual_populated_in_live_mode(self) -> None:
        """GIVEN live mode with mocked sub-issues API returning 2 sub-issues
        WHEN build_plan is called
        THEN github_subissues_actual.items contains the returned sub-issues.

        AC3: subissues_actual in live mode.
        """
        mock_subissues = [
            {"number": 281, "title": "docs", "state": "OPEN", "url": "https://..."},
            {"number": 285, "title": "feat", "state": "OPEN", "url": "https://..."},
        ]
        view_effects = {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=mock_subissues,
        )
        assert plan.github_subissues_actual is not None
        assert plan.github_subissues_actual.status == "ok"
        assert len(plan.github_subissues_actual.items) == 2

    def test_yaml_contains_body_inventory_and_subissues_actual(self) -> None:
        """GIVEN a plan
        WHEN plan_to_yaml is called
        THEN output contains body_inventory: and github_subissues_actual: sections.

        AC3: yaml serialization of both fields.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "body_inventory:" in yaml_output
        assert "github_subissues_actual:" in yaml_output


# ---------------------------------------------------------------------------
# Tests: AC4 — register_subissue_or_human_escalation
# ---------------------------------------------------------------------------


class TestRegisterSubissue:
    """AC4: body has #N but native Sub-issue parent read-back doesn't match → register_subissue."""

    def test_register_subissue_when_not_in_subissues_actual(self) -> None:
        """GIVEN body has #281 for C254-1, but native sub-issues list is empty
        WHEN build_plan is called (live mode with subissues=[])
        THEN C254-1 action is register_subissue_or_human_escalation.

        AC4: register_subissue.
        """
        view_effects = {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }
        # subissues_actual is empty — #281 is not registered as native sub-issue
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[],  # no native sub-issues registered
        )
        child = _child_by_id(plan, "C254-1")
        assert child.action == "register_subissue_or_human_escalation", (
            f"Expected register_subissue_or_human_escalation, got {child.action}"
        )

    def test_no_op_when_issue_registered_as_subissue(self) -> None:
        """GIVEN body has #281 for C254-1, and native sub-issues contains #281
        WHEN build_plan is called
        THEN C254-1 action is no_op.

        AC4: when subissue is properly registered, no_op.
        """
        view_effects = {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }
        # subissues_actual contains #281 — properly registered
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[{"number": 281, "title": "docs", "state": "OPEN", "url": ""}],
        )
        child = _child_by_id(plan, "C254-1")
        assert child.action == "no_op", (
            f"Expected no_op when sub-issue is registered, got {child.action}"
        )

    def test_required_subissue_registrations_populated(self) -> None:
        """GIVEN body references #281 but it's not in subissues_actual
        WHEN build_plan is called
        THEN plan.required_subissue_registrations contains an entry for C254-1.

        AC4: required_subissue_registrations.
        """
        view_effects = {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[],
        )
        # Should have registrations for C254-1 (and possibly C254-5 after stale update)
        assert len(plan.required_subissue_registrations) > 0


# ---------------------------------------------------------------------------
# Tests: child line parsing
# ---------------------------------------------------------------------------


class TestParseChildLines:
    def test_detects_placeholder_line(self) -> None:
        """GIVEN a child line with （未起票）
        WHEN _parse_child_lines is called
        THEN is_placeholder is True.
        """
        body = "## Child Issues\n\n- C254-3 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-3"
        assert results[0]["is_placeholder"] is True
        assert results[0]["raw_issue_refs"] == []

    def test_detects_issue_ref_line(self) -> None:
        """GIVEN a child line with an issue reference like #281
        WHEN _parse_child_lines is called
        THEN raw_issue_refs contains 281.
        """
        body = "## Child Issues\n\n- C254-1 docs: something #281\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-1"
        assert results[0]["is_placeholder"] is False
        assert 281 in results[0]["raw_issue_refs"]

    def test_detects_stale_line_with_both_placeholder_and_ref(self) -> None:
        """GIVEN a line with both （未起票） and #285
        WHEN _parse_child_lines is called
        THEN is_placeholder is True AND raw_issue_refs contains 285.
        """
        body = "## Child Issues\n\n- C254-5 feat: something（未起票） #285\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["is_placeholder"] is True
        assert 285 in results[0]["raw_issue_refs"]

    def test_ignores_non_child_lines_in_section(self) -> None:
        """GIVEN a body with no Cxxx-N lines in Child Issues section
        WHEN _parse_child_lines is called
        THEN results is empty.
        """
        body = "## Child Issues\n\nSome text without child references.\n"
        assert pmc._parse_child_lines(body) == []

    def test_checkbox_form_parsed(self) -> None:
        """GIVEN a child line with checkbox form '- [ ] #N — CX-M: ...'
        WHEN _parse_child_lines is called
        THEN child_id is correctly extracted.
        """
        body = "## Child Issues\n\n- [ ] #281 — C254-1: docs: something\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-1"


# ---------------------------------------------------------------------------
# Tests: classify_child — live mode with mock
# ---------------------------------------------------------------------------


class TestClassifyChildMissing:
    """Classification: missing."""

    def test_missing_when_placeholder_no_ref(self) -> None:
        """GIVEN a child line with （未起票） and no issue ref
        WHEN classified
        THEN status is 'missing' and action is 'create_issue'.
        """
        parsed = {
            "child_id": "C254-3",
            "rest": "docs: something（未起票）",
            "is_placeholder": True,
            "raw_issue_refs": [],
            "line_number": 5,
            "raw_line": "- C254-3 docs: something（未起票）",
        }
        warnings: list[str] = []
        entry = pmc._classify_child(
            parsed,
            parent_issue=254,
            parent_mode="delivery-rollup",
            repo=TRUSTED_REPO,
            dry_run=False,
            issue_lookup_warnings=warnings,
            subissues_actual=[],
        )
        assert entry.status == "missing"
        assert entry.action == "create_issue"
        assert entry.existing_issue is None
        assert entry.dedupe_key == "delivery-rollup:254:C254-3"

    def test_missing_when_no_placeholder_and_no_ref(self) -> None:
        """GIVEN a child line with no placeholder and no issue ref
        WHEN classified
        THEN status is 'missing' (bare description).
        """
        parsed = {
            "child_id": "C254-7",
            "rest": "docs: some description without ref",
            "is_placeholder": False,
            "raw_issue_refs": [],
            "line_number": 6,
            "raw_line": "- C254-7 docs: some description without ref",
        }
        warnings: list[str] = []
        entry = pmc._classify_child(
            parsed,
            parent_issue=254,
            parent_mode="delivery-rollup",
            repo="",
            dry_run=True,
            issue_lookup_warnings=warnings,
            subissues_actual=[],
        )
        assert entry.status == "missing"
        assert entry.action == "create_issue"


class TestClassifyChildExistingOpen:
    """Classification: existing_open."""

    def test_existing_open_when_ref_matches_open_issue_and_registered(self) -> None:
        """GIVEN a child line referencing a known open issue #281 that is registered
        WHEN classified (live mode, issue is OPEN, in subissues_actual)
        THEN status is 'existing_open' and action is 'no_op'.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
            "line_number": 4,
            "raw_line": "- C254-1 docs: something #281",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(281, "OPEN")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo=TRUSTED_REPO,
                dry_run=False,
                issue_lookup_warnings=warnings,
                subissues_actual=[{"number": 281, "state": "OPEN", "title": "docs", "url": ""}],
            )
        assert entry.status == "existing_open"
        assert entry.action == "no_op"
        assert entry.existing_issue is not None
        assert entry.existing_issue.number == 281

    def test_existing_open_dedupe_key_format(self) -> None:
        """GIVEN a child classified as existing_open
        WHEN checking dedupe_key
        THEN it follows 'delivery-rollup:<parent>:<child_id>' format.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
            "line_number": 4,
            "raw_line": "- C254-1 docs: something #281",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(281, "OPEN")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo=TRUSTED_REPO,
                dry_run=False,
                issue_lookup_warnings=warnings,
                subissues_actual=[{"number": 281, "state": "OPEN", "title": "docs", "url": ""}],
            )
        assert entry.dedupe_key == "delivery-rollup:254:C254-1"


class TestClassifyChildExistingClosed:
    """Classification: existing_closed — closed child must NOT be ambiguous."""

    def test_closed_child_is_existing_closed_not_ambiguous(self) -> None:
        """GIVEN a child line referencing a closed issue
        WHEN classified (live mode, issue is CLOSED)
        THEN status is 'existing_closed' (not 'ambiguous') and action is 'no_op'.

        Regression: Blocker 1 — closed child was previously classified as ambiguous.
        """
        parsed = {
            "child_id": "C254-1",
            "rest": "docs: something #281",
            "is_placeholder": False,
            "raw_issue_refs": [281],
            "line_number": 4,
            "raw_line": "- C254-1 docs: something #281",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(281, "CLOSED", "COMPLETED")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo=TRUSTED_REPO,
                dry_run=False,
                issue_lookup_warnings=warnings,
                subissues_actual=[{"number": 281, "state": "CLOSED", "title": "docs", "url": ""}],
            )
        assert entry.status == "existing_closed", (
            "closed child must be 'existing_closed', not 'ambiguous'"
        )
        assert entry.action == "no_op"
        assert entry.existing_issue is not None
        assert entry.existing_issue.state == "CLOSED"


class TestClassifyChildStaleBodyOnly:
    """Classification: stale_body_only."""

    def test_stale_body_only_when_placeholder_and_open_ref(self) -> None:
        """GIVEN a child line with （未起票） AND a reference to an open issue #285
        WHEN classified (live mode)
        THEN status is 'stale_body_only' and action is 'reuse_and_update_parent'.
        """
        parsed = {
            "child_id": "C254-5",
            "rest": "feat: SDD 採否 ADR を追加（未起票） #285",
            "is_placeholder": True,
            "raw_issue_refs": [285],
            "line_number": 6,
            "raw_line": "- C254-5 feat: SDD 採否 ADR を追加（未起票） #285",
        }
        warnings: list[str] = []
        with patch.object(
            pmc, "_view_issue", return_value=_make_issue_info(285, "OPEN")
        ), patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo=TRUSTED_REPO,
                dry_run=False,
                issue_lookup_warnings=warnings,
                subissues_actual=[],
            )
        assert entry.status == "stale_body_only"
        assert entry.action == "reuse_and_update_parent"
        assert entry.existing_issue is not None
        assert entry.existing_issue.number == 285


class TestClassifyChildAmbiguous:
    """Classification: ambiguous."""

    def test_ambiguous_when_view_issue_fails(self) -> None:
        """GIVEN a child line referencing an issue where gh issue view fails
        WHEN classified (live mode)
        THEN status is 'ambiguous', action is 'human_escalation',
             and a warning is recorded.

        Regression: Blocker 2 — lookup failure must not be a silent fallback.
        """
        parsed = {
            "child_id": "C254-9",
            "rest": "feat: something #999",
            "is_placeholder": False,
            "raw_issue_refs": [999],
            "line_number": 7,
            "raw_line": "- C254-9 feat: something #999",
        }
        warnings: list[str] = []
        with patch.object(pmc, "_view_issue", return_value=None), \
             patch.object(pmc, "_search_dedupe_candidates", return_value=[]):
            entry = pmc._classify_child(
                parsed,
                parent_issue=254,
                parent_mode="delivery-rollup",
                repo=TRUSTED_REPO,
                dry_run=False,
                issue_lookup_warnings=warnings,
                subissues_actual=[],
            )
        assert entry.status == "ambiguous"
        assert entry.action == "human_escalation"
        assert len(warnings) > 0, "A warning must be recorded when issue view fails"


class TestParentModeUnknown:
    """Classification when parent_mode is unknown — Blocker 4."""

    def test_unknown_parent_mode_produces_human_escalation(self) -> None:
        """GIVEN parent_mode='unknown' (broken contract)
        WHEN classify_child is called
        THEN action is 'human_escalation'.

        Regression: Blocker 4 — unknown parent_mode must not default to delivery-rollup.
        """
        parsed = {
            "child_id": "C254-3",
            "rest": "docs: something（未起票）",
            "is_placeholder": True,
            "raw_issue_refs": [],
            "line_number": 5,
            "raw_line": "- C254-3 docs: something（未起票）",
        }
        warnings: list[str] = []
        entry = pmc._classify_child(
            parsed,
            parent_issue=254,
            parent_mode="unknown",
            repo="",
            dry_run=True,
            issue_lookup_warnings=warnings,
            subissues_actual=[],
        )
        assert entry.action == "human_escalation"

    def test_build_plan_with_missing_parent_mode(self) -> None:
        """GIVEN a body without parent_mode key
        WHEN build_plan is called (dry_run)
        THEN parent_mode is 'unknown' in the plan.

        Regression: Blocker 4 — must return 'unknown', not 'delivery-rollup'.
        """
        body = """\
## Outcome

No machine-readable contract here.

## Child Issues

- C254-3 docs: something（未起票）
"""
        plan = _build_dry_run(body, parent_issue=254)
        assert plan.parent_mode == "unknown"


# ---------------------------------------------------------------------------
# Tests: full build_plan with #254 fixture (dry-run)
# ---------------------------------------------------------------------------


class TestBuildPlan254FixtureDryRun:
    """Integration-level tests using the #254-equivalent fixture (dry-run mode)."""

    def test_parent_mode_is_delivery_rollup(self) -> None:
        """GIVEN the #254 fixture body
        WHEN build_plan is called (dry_run)
        THEN parent_mode is 'delivery-rollup'.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.parent_mode == "delivery-rollup"

    def test_closure_mode_is_child_complete(self) -> None:
        """GIVEN the #254 fixture body with closure_mode: child-complete
        WHEN build_plan is called (dry_run)
        THEN closure_mode is 'child-complete'.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.closure_mode == "child-complete"

    def test_parent_issue_number(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.parent_issue == 254

    def test_body_sha256_present(self) -> None:
        """GIVEN the #254 fixture body
        WHEN build_plan is called
        THEN body_sha256 is a non-empty hex string.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert len(plan.body_sha256) == 64
        assert all(c in "0123456789abcdef" for c in plan.body_sha256)

    def test_schema_version_is_2(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.schema_version == 2

    def test_issue_lookup_complete_in_dry_run(self) -> None:
        """In dry-run mode, issue_lookup.complete is True (trivially)."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert plan.issue_lookup.complete is True

    def test_c254_3_is_missing(self) -> None:
        """GIVEN C254-3 has （未起票） and no issue ref
        WHEN classified via build_plan (dry_run)
        THEN status is 'missing'.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        child = _child_by_id(plan, "C254-3")
        assert child.status == "missing"
        assert child.existing_issue is None
        assert child.action == "create_issue"

    def test_three_children_detected(self) -> None:
        """GIVEN the fixture has exactly 3 child lines in ## Child Issues
        WHEN build_plan is called (dry_run)
        THEN plan.children has length 3.

        Note: C254-9 mentioned in Background section must NOT be parsed.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert len(plan.children) == 3

    def test_background_child_id_not_parsed(self) -> None:
        """GIVEN C254-9 is mentioned in ## Background (not ## Child Issues)
        WHEN build_plan is called
        THEN C254-9 is NOT in plan.children.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        child_ids = [c.child_id for c in plan.children]
        assert "C254-9" not in child_ids

    def test_required_issue_creations_contains_missing(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert "C254-3" in plan.required_issue_creations


# ---------------------------------------------------------------------------
# Tests: full build_plan with live mode (mocked gh)
# ---------------------------------------------------------------------------


class TestBuildPlan254FixtureLive:
    """Integration-level tests using the #254-equivalent fixture (live mode, mocked)."""

    def _make_view_effects(self) -> dict:
        return {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }

    def test_c254_1_is_existing_open_with_registered_subissue(self) -> None:
        """GIVEN C254-1 references open issue #281, registered as sub-issue
        WHEN classified via build_plan (live, mocked)
        THEN status is 'existing_open', action is 'no_op'.
        """
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=self._make_view_effects(),
            subissues_actual=[
                {"number": 281, "state": "OPEN", "title": "docs", "url": ""},
                {"number": 285, "state": "OPEN", "title": "feat", "url": ""},
            ],
        )
        child = _child_by_id(plan, "C254-1")
        assert child.status == "existing_open"
        assert child.action == "no_op"

    def test_c254_5_is_stale_body_only(self) -> None:
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=self._make_view_effects(),
            subissues_actual=[],
        )
        child = _child_by_id(plan, "C254-5")
        assert child.status == "stale_body_only"
        assert child.action == "reuse_and_update_parent"

    def test_closed_referenced_issue_is_existing_closed(self) -> None:
        """GIVEN C254-1 references a CLOSED issue #281
        WHEN classified via build_plan (live, mocked)
        THEN status is 'existing_closed', NOT 'ambiguous'.

        Regression: Blocker 1 — closed children must not be ambiguous.
        """
        view_effects = {
            281: _make_issue_info(281, "CLOSED", "COMPLETED"),
            285: _make_issue_info(285, "OPEN"),
        }
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[{"number": 281, "state": "CLOSED", "title": "docs", "url": ""}],
        )
        child = _child_by_id(plan, "C254-1")
        assert child.status == "existing_closed", (
            "closed referenced issue must be 'existing_closed', NOT 'ambiguous'"
        )
        assert child.action == "no_op"

    def test_required_issue_edits_contains_stale(self) -> None:
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=self._make_view_effects(),
            subissues_actual=[],
        )
        assert len(plan.required_issue_edits) > 0

    def test_parent_body_updates_for_stale_child(self) -> None:
        """GIVEN C254-5 is stale_body_only
        WHEN build_plan is called (live, mocked)
        THEN parent_body_updates contains a safe patch entry for C254-5.
        """
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=self._make_view_effects(),
            subissues_actual=[],
        )
        stale_updates = [u for u in plan.parent_body_updates if "C254-5" in u.old_line]
        assert len(stale_updates) == 1
        upd = stale_updates[0]
        assert "（未起票）" not in upd.new_line
        assert "#285" in upd.new_line
        # Verify safe patch fields (AC7: old_line/new_line/expected_match_count)
        assert upd.section == "Child Issues"
        assert upd.line_number > 0
        assert upd.expected_match_count >= 1

    def test_parent_body_updates_expected_match_count_unique(self) -> None:
        """GIVEN each stale child line appears exactly once in the body
        WHEN build_plan is called
        THEN expected_match_count is 1 for each update.
        """
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=self._make_view_effects(),
            subissues_actual=[],
        )
        for upd in plan.parent_body_updates:
            assert upd.expected_match_count == 1, (
                f"expected_match_count must be 1 for unique lines, got {upd.expected_match_count} "
                f"for: {upd.old_line!r}"
            )

    def test_parent_body_updates_duplicate_match_detected(self) -> None:
        """GIVEN a body with a duplicate stale child line
        WHEN build_plan is called
        THEN expected_match_count reflects the actual count (>1).

        Regression: Blocker 7 — duplicate matches must be detectable.
        """
        # Duplicate the stale line
        duplicate_body = FIXTURE_PARENT_BODY_254 + (
            "- C254-5 feat: SDD 採否 ADR を追加（未起票） #285\n"
        )
        view_effects = self._make_view_effects()
        plan = _build_live(
            duplicate_body,
            view_side_effects=view_effects,
            subissues_actual=[],
        )
        # With a duplicate stale line for C254-5 in the body, we expect TWO children
        c254_5_updates = [u for u in plan.parent_body_updates if "C254-5" in u.old_line]
        # The duplicate body causes expected_match_count > 1 for the first occurrence
        # OR we get two separate update entries; either way consumer can detect the issue
        total_matches = sum(u.expected_match_count for u in c254_5_updates)
        assert total_matches > 1, (
            "duplicate stale lines must result in expected_match_count > 1 to be detectable"
        )


# ---------------------------------------------------------------------------
# Regression: Blocker 2 — gh issue list failure must not silently make all refs ambiguous
# ---------------------------------------------------------------------------


class TestIssueLookupFailure:
    def test_view_issue_failure_records_warning_not_fatal(self) -> None:
        """GIVEN gh issue view fails for one issue
        WHEN build_plan is called
        THEN that child is ambiguous with a warning recorded in issue_lookup.warnings.

        Regression: Blocker 2 — per-issue failure is isolated (not plan-level).
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- C254-9 feat: something #999
"""
        with patch.object(pmc, "_view_issue", return_value=None), \
             patch.object(pmc, "_search_dedupe_candidates", return_value=[]), \
             patch.object(pmc, "_fetch_subissues_actual",
                          return_value=pmc.SubissuesReadback(status="ok", items=[], complete=True)):
            plan = pmc.build_plan(
                body, parent_issue=254, repo=TRUSTED_REPO, dry_run=False
            )
        child = _child_by_id(plan, "C254-9")
        assert child.status == "ambiguous"
        assert child.action == "human_escalation"
        assert len(plan.issue_lookup.warnings) > 0
        # Plan itself remains complete (per-issue failure, not plan-level)
        assert plan.issue_lookup.complete is True


# ---------------------------------------------------------------------------
# Regression: Blocker 8 — --body-file uses existing_unverified, not ambiguous
# ---------------------------------------------------------------------------


class TestDryRunBodyFile:
    def test_body_file_mode_uses_existing_unverified(self) -> None:
        """GIVEN --body-file mode (dry_run=True) and a child with an issue ref
        WHEN build_plan is called
        THEN status is 'existing_unverified' (NOT 'ambiguous').

        Regression: Blocker 8 — body-file mode must not classify all refs as ambiguous.
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- C254-1 docs: something #281
"""
        plan = _build_dry_run(body, parent_issue=254)
        child = _child_by_id(plan, "C254-1")
        assert child.status == "existing_unverified", (
            "dry-run mode must use 'existing_unverified', not 'ambiguous'"
        )

    def test_body_file_mode_stale_is_stale_not_ambiguous(self) -> None:
        """GIVEN --body-file mode and a stale child (placeholder + ref)
        WHEN build_plan is called
        THEN status is 'stale_body_only' (not 'ambiguous').
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- C254-5 feat: something（未起票） #285
"""
        plan = _build_dry_run(body, parent_issue=254)
        child = _child_by_id(plan, "C254-5")
        assert child.status == "stale_body_only"


# ---------------------------------------------------------------------------
# Tests: V2 schema fields in plan
# ---------------------------------------------------------------------------


class TestPlanV2Schema:
    """Verify CHILD_MATERIALIZATION_PLAN_V2 schema fields — Blocker 5."""

    def test_schema_has_closure_mode(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert hasattr(plan, "closure_mode")
        assert plan.closure_mode == "child-complete"

    def test_schema_has_repo(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254, repo=TRUSTED_REPO)
        assert plan.repo == TRUSTED_REPO

    def test_schema_has_source_fields(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254, parent_issue=254)
        assert plan.source_issue_number == 254
        assert len(plan.body_sha256) == 64

    def test_schema_has_issue_lookup(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        assert hasattr(plan, "issue_lookup")
        assert hasattr(plan.issue_lookup, "complete")
        assert hasattr(plan.issue_lookup, "strategy")
        assert hasattr(plan.issue_lookup, "warnings")

    def test_child_has_existing_issue_not_number(self) -> None:
        """Children must have 'existing_issue' (ExistingIssueInfo), not plain int."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        child = _child_by_id(plan, "C254-1")
        # existing_issue is an ExistingIssueInfo object (or None), not an int
        if child.existing_issue is not None:
            assert hasattr(child.existing_issue, "number")
            assert hasattr(child.existing_issue, "state")

    def test_child_has_existing_issue_candidates(self) -> None:
        """Each child must have an existing_issue_candidates list."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        for child in plan.children:
            assert hasattr(child, "existing_issue_candidates")
            assert isinstance(child.existing_issue_candidates, list)


# ---------------------------------------------------------------------------
# Tests: YAML serialization
# ---------------------------------------------------------------------------


class TestPlanToYaml:
    def test_output_starts_with_schema_key_v2(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert yaml_output.startswith("CHILD_MATERIALIZATION_PLAN_V2:")

    def test_output_contains_missing_status(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "missing" in yaml_output

    def test_output_contains_existing_unverified_for_dry_run(self) -> None:
        """In dry-run mode, existing refs are serialized as 'existing_unverified'."""
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "existing_unverified" in yaml_output

    def test_output_contains_stale_body_only_status(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "stale_body_only" in yaml_output

    def test_output_contains_dedupe_key(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "delivery-rollup:254:C254-3" in yaml_output

    def test_output_contains_closure_mode(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "closure_mode: child-complete" in yaml_output

    def test_output_contains_issue_lookup(self) -> None:
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "issue_lookup:" in yaml_output
        assert "complete:" in yaml_output

    def test_parent_body_updates_have_safe_patch_fields(self) -> None:
        """Serialized parent_body_updates must include safe patch fields (AC7 schema)."""
        view_effects = {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[],
        )
        yaml_output = pmc.plan_to_yaml(plan)
        if plan.parent_body_updates:
            assert "section:" in yaml_output
            assert "line_number:" in yaml_output
            assert "old_line:" in yaml_output
            assert "new_line:" in yaml_output
            assert "expected_match_count:" in yaml_output

    def test_empty_plan_no_warnings(self) -> None:
        """GIVEN an empty body (no child lines in Child Issues section)
        WHEN plan_to_yaml is called
        THEN the output is still valid YAML (with a warning).
        """
        plan = _build_dry_run("## Outcome\n\nNo child lines here.\n")
        yaml_output = pmc.plan_to_yaml(plan)
        assert "CHILD_MATERIALIZATION_PLAN_V2:" in yaml_output
        # A warning should be present about no children found
        assert "warnings:" in yaml_output

    def test_output_contains_body_inventory_and_subissues(self) -> None:
        """GIVEN any plan
        WHEN plan_to_yaml is called
        THEN output contains body_inventory and github_subissues_actual sections.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_254)
        yaml_output = pmc.plan_to_yaml(plan)
        assert "body_inventory:" in yaml_output
        assert "github_subissues_actual:" in yaml_output


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_bullet_form_child_lines_parsed(self) -> None:
        """GIVEN child lines prefixed with '- '
        WHEN _parse_child_lines is called (within ## Child Issues)
        THEN they are parsed correctly.
        """
        body = "## Child Issues\n\n- C254-2 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-2"

    def test_non_bullet_child_lines_parsed(self) -> None:
        """GIVEN child lines without bullet prefix
        WHEN _parse_child_lines is called (within ## Child Issues)
        THEN they are parsed correctly.
        """
        body = "## Child Issues\n\nC254-4 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert len(results) == 1
        assert results[0]["child_id"] == "C254-4"

    def test_line_number_is_tracked(self) -> None:
        """GIVEN a body with Child Issues section
        WHEN _parse_child_lines is called
        THEN each result has a line_number >= 1.
        """
        body = "## Child Issues\n\n- C254-3 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert results[0]["line_number"] >= 1

    def test_raw_line_is_tracked(self) -> None:
        """GIVEN a body with a child line
        WHEN _parse_child_lines is called
        THEN raw_line contains the original line text.
        """
        body = "## Child Issues\n\n- C254-3 docs: something（未起票）\n"
        results = pmc._parse_child_lines(body)
        assert "C254-3" in results[0]["raw_line"]


# ---------------------------------------------------------------------------
# Tests: Blocker 1 — _CHILD_LINE_RE limited to [A-D], not [A-Z]
# ---------------------------------------------------------------------------


class TestChildLineReAbcdLimited:
    """Blocker 1: _CHILD_LINE_RE_ABCD accepts only A/B/C/D, not arbitrary uppercase letters."""

    def test_e_through_z_not_parsed(self) -> None:
        """GIVEN lines like '- [ ] E: ...' or '- [ ] Z: ...'
        WHEN _parse_child_lines is called
        THEN they are NOT parsed (only A-D allowed).
        """
        body = """\
## Child Issues

- [ ] E: should not be parsed
- [ ] F: also not parsed
- [ ] Z: nor this
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "E" not in child_ids
        assert "F" not in child_ids
        assert "Z" not in child_ids

    def test_a_b_c_d_still_parsed(self) -> None:
        """GIVEN lines with A/B/C/D track IDs (colon form)
        WHEN _parse_child_lines is called
        THEN they ARE parsed.
        """
        body = """\
## Child Issues

- [ ] A: valid entry — #327
- [ ] B: valid entry — #329
- [ ] C: valid entry — #330
- [ ] D: valid entry — #331
"""
        results = pmc._parse_child_lines(body)
        child_ids = [r["child_id"] for r in results]
        assert "A" in child_ids
        assert "B" in child_ids
        assert "C" in child_ids
        assert "D" in child_ids


# ---------------------------------------------------------------------------
# Tests: Blocker 2 — nested bullets (absorbs/output/validates) are NOT candidates
# ---------------------------------------------------------------------------

#: Fixture that matches the real #244 structure with nested metadata bullets.
FIXTURE_PARENT_BODY_244_WITH_NESTED_METADATA = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

### 実装トラック

- [ ] A: Issue body validator — #327
  - absorbs: #46, #57
  - output: validate_issue_body.py
- [ ] B: baseline_vc_preflight.py 実装 — #329
  - absorbs: #100
  - validates: preflight conditions
- [ ] C: validate_pr_body.py 実装 — #330
  - output: validate_pr_body.py
- [ ] D: text/markdown lint 導入 — #331
  - optional: true

## Background

Some background.
"""


class TestNestedBulletNotCandidate:
    """Blocker 2: nested bullet lines (absorbs/output/validates) must NOT be candidates."""

    def test_nested_bullets_not_counted_as_candidates(self) -> None:
        """GIVEN #244-style body with nested metadata under A/B/C/D entries
        WHEN build_plan is called
        THEN candidate_count == 4 (only A/B/C/D, not absorbs/output/validates/optional).

        Blocker 2: nested metadata bullets are ignored as child candidates.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_244_WITH_NESTED_METADATA, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.candidate_count == 4, (
            f"Expected 4 candidates (A/B/C/D only), got {bi.candidate_count}. "
            "Nested bullets (absorbs/output/validates/optional) must not be counted."
        )
        assert bi.parsed_count == 4, (
            f"Expected 4 parsed, got {bi.parsed_count}"
        )

    def test_nested_bullets_produce_no_parser_gap(self) -> None:
        """GIVEN #244-style body with nested metadata under A/B/C/D entries
        WHEN build_plan is called
        THEN parser_gap_report is empty (no false gaps from nested bullets).

        Blocker 2: nested metadata must not generate parser_gap entries.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_244_WITH_NESTED_METADATA, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.parser_gap_report == [], (
            f"Expected empty parser_gap_report, got: {bi.parser_gap_report}"
        )

    def test_nested_bullets_children_still_parsed(self) -> None:
        """GIVEN #244-style body with nested metadata
        WHEN build_plan is called
        THEN all 4 A/B/C/D children are in plan.children.

        Blocker 2: nested bullets must not interfere with top-level child parsing.
        """
        plan = _build_dry_run(FIXTURE_PARENT_BODY_244_WITH_NESTED_METADATA, parent_issue=244)
        child_ids = [c.child_id for c in plan.children]
        assert "A" in child_ids
        assert "B" in child_ids
        assert "C" in child_ids
        assert "D" in child_ids
        assert len(plan.children) == 4


# ---------------------------------------------------------------------------
# Tests: Blocker 3 — A/B/C/D colon-required build_plan() integration path
# ---------------------------------------------------------------------------


class TestAbcdColonRequired:
    """Blocker 3: A/B/C/D without colon must produce parser_gap via build_plan()."""

    def test_abcd_no_colon_is_parser_gap_not_parsed(self) -> None:
        """GIVEN '- [ ] A Title' (no colon after A)
        WHEN build_plan is called
        THEN candidate_count=1, parsed_count=0 (colon missing = not parsed).

        Blocker 3: colon-less A/B/C/D is a parser gap, not a valid child.
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- [ ] A Issue body validator
"""
        plan = _build_dry_run(body, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.candidate_count == 1, f"Expected 1 candidate, got {bi.candidate_count}"
        assert bi.parsed_count == 0, (
            f"Expected 0 parsed (colon missing), got {bi.parsed_count}"
        )

    def test_abcd_no_colon_produces_high_confidence_repair(self) -> None:
        """GIVEN '- [ ] A Title' (no colon after A)
        WHEN build_plan is called
        THEN parser_gap has repair_confidence=high and suggested_repair contains 'A:'.

        Blocker 3: AC2c integration — high repair_confidence routed to issue-author.
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- [ ] A Issue body validator
"""
        plan = _build_dry_run(body, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        assert len(bi.parser_gap_report) == 1, (
            f"Expected 1 parser gap entry, got {len(bi.parser_gap_report)}"
        )
        gap = bi.parser_gap_report[0]
        assert gap.repair_confidence == "high", (
            f"Expected repair_confidence=high, got {gap.repair_confidence}"
        )
        assert gap.suggested_repair is not None
        assert "A:" in gap.suggested_repair, (
            f"Expected suggested_repair to contain 'A:', got: {gap.suggested_repair!r}"
        )

    def test_abcd_with_colon_parses_correctly(self) -> None:
        """GIVEN '- [ ] A: Title' (colon present)
        WHEN build_plan is called
        THEN parsed_count=1 and no parser_gap.

        Blocker 3: colon-present form must still parse correctly.
        """
        body = """\
## Machine-Readable Contract

```yaml
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Child Issues

- [ ] A: Issue body validator
"""
        plan = _build_dry_run(body, parent_issue=244)
        bi = plan.body_inventory
        assert bi is not None
        assert bi.parsed_count == 1, f"Expected 1 parsed, got {bi.parsed_count}"
        assert bi.parser_gap_report == [], (
            f"Expected empty parser_gap_report, got: {bi.parser_gap_report}"
        )


# ---------------------------------------------------------------------------
# Tests: Blocker 4 — SubissuesReadback fail-closed design
# ---------------------------------------------------------------------------


class TestSubissuesReadbackFailClosed:
    """Blocker 4: _fetch_subissues_actual returns SubissuesReadback; API errors are fail-closed."""

    def test_subissues_readback_ok_status(self) -> None:
        """GIVEN successful API response
        WHEN _fetch_subissues_actual is called (mocked)
        THEN SubissuesReadback.status == 'ok' and complete == True.

        Blocker 4: ok status with items.
        """
        # Mock via build_plan using _build_live helper
        mock_items = [{"number": 281, "title": "docs", "state": "OPEN", "url": ""}]
        view_effects = {281: _make_issue_info(281, "OPEN")}
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=mock_items,
            subissues_readback_status="ok",
        )
        assert plan.github_subissues_actual is not None
        assert plan.github_subissues_actual.status == "ok"
        assert plan.github_subissues_actual.complete is True

    def test_api_error_routes_to_human_escalation(self) -> None:
        """GIVEN SubissuesReadback with status='forbidden' (API error)
        WHEN build_plan is called
        THEN issue_lookup.complete=False AND children needing registration get human_escalation.

        Blocker 4: fail-closed — API error must not produce mutation plan.
        """
        view_effects = {
            281: _make_issue_info(281, "OPEN"),
            285: _make_issue_info(285, "OPEN"),
        }
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[],
            subissues_readback_status="forbidden",
        )
        # Plan lookup must be incomplete when readback failed
        assert plan.issue_lookup.complete is False
        # Children that would need register_subissue must be routed to human_escalation
        for child in plan.children:
            assert child.action != "register_subissue_or_human_escalation", (
                f"Child {child.child_id} must not be register_subissue when readback is incomplete"
            )

    def test_api_error_adds_warning(self) -> None:
        """GIVEN SubissuesReadback with status='not_found'
        WHEN build_plan is called
        THEN plan.warnings contains a message about incomplete readback.

        Blocker 4: consumer must be notified that actual state is unknown.
        """
        view_effects = {281: _make_issue_info(281, "OPEN")}
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[],
            subissues_readback_status="not_found",
        )
        warning_text = " ".join(plan.warnings)
        assert "complete=false" in warning_text or "complete" in warning_text.lower(), (
            "Expected a warning about incomplete readback"
        )

    def test_subissues_readback_complete_false_in_yaml(self) -> None:
        """GIVEN API error readback
        WHEN plan_to_yaml is called
        THEN YAML contains 'complete: false' under github_subissues_actual.

        Blocker 4: YAML output must expose incomplete status for consumers.
        """
        view_effects = {281: _make_issue_info(281, "OPEN")}
        plan = _build_live(
            FIXTURE_PARENT_BODY_254,
            view_side_effects=view_effects,
            subissues_actual=[],
            subissues_readback_status="api_error",
        )
        yaml_output = pmc.plan_to_yaml(plan)
        assert "complete: false" in yaml_output, (
            "YAML must contain 'complete: false' when readback failed"
        )


# ---------------------------------------------------------------------------
# Tests: Blocker 7 — hidden/Bidi Unicode scan
# ---------------------------------------------------------------------------


class TestHiddenBidiUnicode:
    """Blocker 7: no hidden/Bidi Unicode control characters in target files."""

    _TARGETS = [
        ".claude/skills/create-issue/scripts/plan_child_materialization.py",
        ".claude/skills/create-issue/tests/test_plan_child_materialization.py",
        ".claude/agents/issue-author.md",
        ".claude/skills/issue-refinement-loop/SKILL.md",
        ".claude/skills/edit-issue/SKILL.md",
    ]

    def test_no_hidden_bidi_chars_in_target_files(self) -> None:
        """GIVEN target script / agent / skill files
        WHEN each file is scanned for hidden/Bidi Unicode control characters
        THEN no Cf/Cc category characters (except newline/tab/CR) are found.

        Blocker 7: Bidi/hidden Unicode warning from GitHub PR view.
        """
        import unicodedata

        repo_root = Path(__file__).parent.parent.parent.parent.parent  # up to repo root

        bad: list[tuple[str, int, str, str]] = []
        for rel_path in self._TARGETS:
            p = repo_root / rel_path
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8")
            for i, ch in enumerate(text):
                cat = unicodedata.category(ch)
                if cat in {"Cf", "Cc"} and ch not in "\n\r\t":
                    bad.append((rel_path, i, hex(ord(ch)), unicodedata.name(ch, "")))

        assert not bad, f"Found hidden/Bidi chars: {bad}"
