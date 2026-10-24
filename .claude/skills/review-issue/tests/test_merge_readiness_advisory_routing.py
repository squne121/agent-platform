"""Tests for `merge_readiness_into_review_result()`'s routing of non-blocking
readiness advisory findings (Issue #2339, PR #2370 OWNER review fix_delta
iteration 1, P0).

`check_extension_surface_advisory()` (in `contract_readiness_check.py`,
issue-contract-review skill) is already covered in isolation by
`.claude/skills/issue-contract-review/tests/test_extension_surface_advisory_routing_parity.py`
as a producer. That coverage alone is insufficient (PR #2370 OWNER review):
before this fix_delta, `merge_readiness_into_review_result()` blindly
converted EVERY entry in `ISSUE_CONTRACT_READINESS_RESULT_V1.errors[]` --
including the non-blocking `category: extension_surface_candidate_advisory`
(EXTSURF003) entry produced by `check_extension_surface_advisory()` -- into
a blocking `structured_blocker` / `blocking_issues` entry, forcing
`verdict: needs-fix` even though the readiness `errors[]` entry was
explicitly non-blocking by design. This file exercises
`merge_readiness_into_review_result()` itself (the actual merge integration
point), not just the readiness producer in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
import check_issue_contract as checker  # noqa: E402

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _clean_review_result() -> dict:
    """A real, schema-valid REVIEW_ISSUE_RESULT_V1 dict with
    verdict: approve / blocking_issues: [] / structured_blockers: []
    (`pass_issue.md`, the same fixture `TestPassCase` in
    `test_check_issue_contract.py` uses to assert `verdict == "approve"`).
    """
    fixture_path = _FIXTURES_DIR / "pass_issue.md"
    body, labels, title = checker.load_fixture_file(str(fixture_path))
    result = checker.run_checks(body, labels=labels, title=title, body_file_path=str(fixture_path))
    payload = checker.result_to_dict(result)
    assert payload["verdict"] == "approve", payload["blocking_issues"]
    assert payload["blocking_issues"] == []
    assert payload["structured_blockers"] == []
    return payload


def _extsurf003_only_readiness_result(body_sha256: str) -> dict:
    """A synthetic ISSUE_CONTRACT_READINESS_RESULT_V1 whose `errors[]`
    contains only the shape `check_extension_surface_advisory()` produces
    (rule_id EXTSURF003, category extension_surface_candidate_advisory,
    severity info) -- non-blocking by construction.
    """
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "go",
        "body_sha256": body_sha256,
        "source_checks": [],
        "errors": [
            {
                "rule_id": "EXTSURF003",
                "severity": "info",
                "source_check": "contract_readiness_check",
                "category": "extension_surface_candidate_advisory",
                "section": "Allowed Paths",
                "line_start": 0,
                "line_end": 0,
                "minimal_context": [
                    "Allowed Path entry '.claude/rules/project-constitution.md' matches the "
                    "project-local extension candidate perimeter glob '.claude/rules/**' "
                    "(unknown_surface_policy.project_candidate_path_globs) but no known "
                    "extension-surface risk-trigger rule selector."
                ],
                "fix_hint": (
                    "Non-blocking: one or more declared Allowed Paths match a project-local "
                    "extension candidate perimeter but no known extension-surface risk-trigger "
                    "rule. No action required."
                ),
                "autofixable": False,
            }
        ],
        "minimal_context": [],
        "fix_hint": None,
    }


def test_candidate_advisory_only_readiness_result_stays_approve_after_merge():
    review_result = _clean_review_result()
    readiness_result = _extsurf003_only_readiness_result(review_result["body_sha256"])

    merged = checker.merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="test_artifact_path/readiness.json",
        iteration_id="test-iteration-1",
    )

    # P0: a non-blocking EXTSURF003-only readiness result must NOT flip a
    # clean review_result's verdict, must NOT add any blocking_issues /
    # structured_blockers, and must NOT set failure_class.
    assert merged["verdict"] == "approve"
    assert merged["blocking_issues"] == []
    assert merged["structured_blockers"] == []
    assert merged.get("failure_class") is None

    # The advisory must still be surfaced -- just non-blockingly.
    assert merged["non_blocking_improvements"]
    codes = {entry.get("code") for entry in merged["non_blocking_improvements"]}
    assert "EXTSURF003" in codes
    advisory_entry = next(
        entry for entry in merged["non_blocking_improvements"] if entry.get("code") == "EXTSURF003"
    )
    assert any("project-constitution.md" in e for e in advisory_entry["evidence"])


def test_blocking_readiness_error_alongside_advisory_still_blocks():
    # Contrast case: a genuinely blocking readiness error alongside an
    # advisory-only one must still block -- this fix_delta narrows the
    # non-blocking routing to the advisory category only, it does not
    # silently widen it to swallow real blockers.
    review_result = _clean_review_result()
    readiness_result = _extsurf003_only_readiness_result(review_result["body_sha256"])
    readiness_result["status"] = "needs_fix"
    readiness_result["errors"].append(
        {
            "rule_id": "EXTSURF001",
            "severity": "error",
            "source_check": "contract_readiness_check",
            "category": "extension_surface_risk_trigger",
            "section": "Runtime Verification Applicability",
            "line_start": 1,
            "line_end": 1,
            "minimal_context": ["synthetic genuine blocker for contrast test"],
            "fix_hint": "synthetic genuine blocker fix_hint",
            "autofixable": False,
        }
    )

    merged = checker.merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="test_artifact_path/readiness.json",
        iteration_id="test-iteration-2",
    )

    assert merged["verdict"] == "needs-fix"
    assert merged["blocking_issues"]
    assert any("synthetic genuine blocker fix_hint" in issue for issue in merged["blocking_issues"])
    # The advisory-category entry must still NOT be present in blocking_issues.
    assert not any(
        "project-constitution.md" in issue for issue in merged["blocking_issues"]
    )
    # ... but it is still surfaced non-blockingly.
    codes = {entry.get("code") for entry in merged["non_blocking_improvements"]}
    assert "EXTSURF003" in codes
