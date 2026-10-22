"""Tests for Issue #2339's non-blocking `unclassified_candidate` advisory
wiring on the `review-issue` (`check_issue_contract.py`) consumer side.

Covers `get_extension_surface_candidate_advisories()` in isolation, and its
wiring into `run_checks()` as a `non_blocking_improvements` entry -- it
must never become a blocking `CheckResult` (Issue #2339 AC6). AC7 parity
with the `issue-contract-review` consumer is covered separately in
`.claude/skills/issue-contract-review/tests/test_extension_surface_advisory_routing_parity.py`
(both files are run together by the Issue's AC7 Verification Command).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
import check_issue_contract as checker  # noqa: E402


_ORDINARY_BODY = """## Allowed Paths

- docs/dev/foo.md
"""

_UNCLASSIFIED_CANDIDATE_BODY = """## Allowed Paths

- .claude/rules/project-constitution.md
"""


def test_get_extension_surface_candidate_advisories_unclassified_candidate_non_empty():
    advisories = checker.get_extension_surface_candidate_advisories(
        _UNCLASSIFIED_CANDIDATE_BODY, "implementation"
    )
    assert advisories
    assert any("project-constitution.md" in advisory for advisory in advisories)


def test_get_extension_surface_candidate_advisories_ordinary_empty():
    advisories = checker.get_extension_surface_candidate_advisories(_ORDINARY_BODY, "implementation")
    assert advisories == []


def test_get_extension_surface_candidate_advisories_non_implementation_issue_kind_empty():
    # Same guard as check_c14_extension_surface_risk_trigger: non-implementation
    # Issues do not declare a Runtime Verification Applicability contract in
    # the same sense, so this is not-applicable for them (parity with C14).
    advisories = checker.get_extension_surface_candidate_advisories(
        _UNCLASSIFIED_CANDIDATE_BODY, "research"
    )
    assert advisories == []


_FULL_FIXTURE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "extension surface advisory routing fixture"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for extension-surface advisory routing fixture testing purposes only.

## Acceptance Criteria

- [ ] AC1: concrete AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- .claude/rules/project-constitution.md

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: advisory routing fixture, no runtime execution needed

## Required Skills

none
"""


def test_run_checks_surfaces_candidate_advisory_as_non_blocking_and_c14_stays_pass():
    result = checker.run_checks(_FULL_FIXTURE_BODY, labels="", title="実装: fixture")

    # C14 (the blocking gate) must remain PASS -- unclassified_candidate is
    # advisory-only and must never force needs_fix/FAIL (Issue #2339 AC6).
    assert result.deterministic_checks.C14_extension_surface_risk_trigger == checker.CheckResult.PASS

    codes = {entry.get("code") for entry in result.non_blocking_improvements}
    assert "extension_surface_candidate_perimeter_advisory" in codes

    advisory_entry = next(
        entry
        for entry in result.non_blocking_improvements
        if entry.get("code") == "extension_surface_candidate_perimeter_advisory"
    )
    assert advisory_entry["severity"] == "advisory"
    assert any("project-constitution.md" in e for e in advisory_entry["evidence"])

    # Non-blocking: no finding tied to this code is marked blocking.
    blocking_findings = [
        f
        for f in result.findings
        if f.get("deterministic_domain_key") == "extension_surface_candidate_perimeter_advisory"
        and f.get("blocking")
    ]
    assert blocking_findings == []
