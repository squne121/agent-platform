"""Issue #2339 AC7 parity test: `review-issue`'s
`get_extension_surface_candidate_advisories()` and `issue-contract-review`'s
`check_extension_surface_advisory()` must return the same non-blocking
`unclassified_candidate` advisory classification for the same fixture body,
because both call the exact same shared evaluator
(`scripts/agent-guards/extension_surface_policy_matcher.py`'s
`evaluate_allowed_paths()`) -- structural parity, not independently
re-derived logic (same pattern as the pre-existing
`test_extension_surface_risk_trigger_parity.py` for the blocking C14 gate).

PR #2370 OWNER review fix_delta (iteration 1, P1 "AC7 consumer parity テスト
が false-green"): the original version of this file only compared the flat
advisory message string lists, never `path_classifications` (the structured
per-entry classification `evaluate_allowed_paths()` actually returns) nor
the final verdict either consumer arrives at. This file now additionally
covers the minimal 4-case test matrix required by the fix_delta: ordinary
path / known hard rule / unclassified candidate / malformed policy -- run
through BOTH consumers, comparing `path_classifications` structurally and
(where applicable) the final verdict, including the verdict AFTER
`merge_readiness_into_review_result()` (Issue #2339 P0 fix) for the
unclassified-candidate case.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CONTRACT_READINESS_SCRIPTS = _HERE.parent / "scripts"


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_consumers(suffix: str):
    check_issue_contract = _load_module_from_path(
        f"check_issue_contract_for_ext_surface_advisory_parity_{suffix}",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        f"contract_readiness_check_for_ext_surface_advisory_parity_{suffix}",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )
    return check_issue_contract, contract_readiness_check


_UNCLASSIFIED_CANDIDATE_FIXTURE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "extension surface advisory routing parity fixture"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for extension-surface advisory routing parity fixture testing only.

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
- reason: advisory routing parity fixture, no runtime execution needed

## Required Skills

none
"""

_ORDINARY_FIXTURE_BODY = _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY.replace(
    "- .claude/rules/project-constitution.md", "- docs/dev/foo.md"
)

_KNOWN_HARD_RULE_FIXTURE_BODY = _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY.replace(
    "- .claude/rules/project-constitution.md", "- .claude/agents/implementation-worker.md"
)


def test_unclassified_candidate_advisory_parity_across_consumers():
    check_issue_contract, contract_readiness_check = _load_consumers("unclassified_legacy")

    review_issue_advisories = check_issue_contract.get_extension_surface_candidate_advisories(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_advisory(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY
    )

    assert review_issue_advisories, "expected a non-empty unclassified_candidate advisory"
    assert len(readiness_errors) == 1
    assert readiness_errors[0]["category"] == "extension_surface_candidate_advisory"
    readiness_advisories = readiness_errors[0]["minimal_context"]

    # Structural parity: same shared-evaluator advisory messages on both sides.
    assert review_issue_advisories == readiness_advisories


def test_ordinary_path_no_advisory_parity_across_consumers():
    check_issue_contract, contract_readiness_check = _load_consumers("ordinary_legacy")

    review_issue_advisories = check_issue_contract.get_extension_surface_candidate_advisories(
        _ORDINARY_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_advisory(_ORDINARY_FIXTURE_BODY)

    assert review_issue_advisories == []
    assert readiness_errors == []


def test_advisory_never_raises_readiness_overall_status():
    """AC6 parity check: the advisory-only fixture must not raise
    `overall_status` above `go` via `run_contract_readiness_check()` --
    `ext_surface_advisory_errors` must stay excluded from the
    `if ext_surface_errors:` escalation."""
    _, contract_readiness_check = _load_consumers("status_legacy")

    validate_result = contract_readiness_check.run_validate_issue_body(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY
    )
    result = contract_readiness_check.build_result(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY, "static", validate_result, None, None
    )
    assert result["status"] == "go"
    categories = {e.get("category") for e in result["errors"]}
    assert "extension_surface_candidate_advisory" in categories
    assert "extension_surface_risk_trigger" not in categories


# ---------------------------------------------------------------------------
# PR #2370 OWNER review fix_delta P1 test matrix: ordinary / known hard rule /
# unclassified candidate / malformed policy, comparing `path_classifications`
# (structured per-entry classification) and final verdict across BOTH
# consumers, not just the flat advisory string list.
# ---------------------------------------------------------------------------


def _evaluator_of(consumer_module):
    """Each consumer dynamically loads its own fresh
    `extension_surface_policy_matcher` module instance
    (`_load_extension_surface_policy_matcher()`, Issue #2290 "Notes for
    Reviewer" no-new-package rule) -- calling `evaluate_allowed_paths()` on
    that instance is exactly what both `check_c14_extension_surface_risk_
    trigger()`/`check_extension_surface_risk_trigger()` and
    `get_extension_surface_candidate_advisories()`/
    `check_extension_surface_advisory()` do internally.
    """
    evaluator = consumer_module._load_extension_surface_policy_matcher()
    assert evaluator is not None
    return evaluator


def test_ordinary_path_classification_parity_and_verdict_approve():
    check_issue_contract, contract_readiness_check = _load_consumers("matrix_ordinary")
    entries = ["docs/dev/foo.md"]

    review_issue_result = _evaluator_of(check_issue_contract).evaluate_allowed_paths(entries)
    readiness_result = _evaluator_of(contract_readiness_check).evaluate_allowed_paths(entries)

    assert review_issue_result["path_classifications"] == readiness_result["path_classifications"]
    assert review_issue_result["path_classifications"] == [
        {"entry": "docs/dev/foo.md", "classification": "ordinary"}
    ]
    assert review_issue_result["final_decision"] is None
    assert readiness_result["final_decision"] is None

    # Final blocking-gate verdict on both consumers: approve/NA (no candidate match).
    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _ORDINARY_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(_ORDINARY_FIXTURE_BODY)
    assert review_issue_status == check_issue_contract.CheckResult.PASS
    assert review_issue_reasons == []
    assert readiness_errors == []


def test_known_hard_rule_classification_parity_and_verdict_needs_fix():
    check_issue_contract, contract_readiness_check = _load_consumers("matrix_hard_rule")
    entries = [".claude/agents/implementation-worker.md"]

    review_issue_result = _evaluator_of(check_issue_contract).evaluate_allowed_paths(entries)
    readiness_result = _evaluator_of(contract_readiness_check).evaluate_allowed_paths(entries)

    assert review_issue_result["path_classifications"] == readiness_result["path_classifications"]
    assert review_issue_result["path_classifications"] == [
        {"entry": ".claude/agents/implementation-worker.md", "classification": "matched_rule"}
    ]
    assert review_issue_result["final_decision"] == readiness_result["final_decision"] == "immediate"

    # Final blocking-gate verdict on both consumers: needs-fix (RVA declares
    # not_applicable while the policy requires immediate).
    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _KNOWN_HARD_RULE_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(
        _KNOWN_HARD_RULE_FIXTURE_BODY
    )
    assert review_issue_status == check_issue_contract.CheckResult.FAIL
    assert review_issue_reasons
    assert readiness_errors
    assert readiness_errors[0]["rule_id"] == "EXTSURF001"


def test_unclassified_candidate_classification_parity_and_merged_verdict_approve():
    check_issue_contract, contract_readiness_check = _load_consumers("matrix_unclassified")
    entries = [".claude/rules/project-constitution.md"]

    review_issue_result = _evaluator_of(check_issue_contract).evaluate_allowed_paths(entries)
    readiness_result = _evaluator_of(contract_readiness_check).evaluate_allowed_paths(entries)

    assert review_issue_result["path_classifications"] == readiness_result["path_classifications"]
    assert len(review_issue_result["path_classifications"]) == 1
    classification = review_issue_result["path_classifications"][0]
    assert classification["classification"] == "unclassified_candidate"
    assert classification["policy_action"] == {"decision": "human_judgment", "gate": "advisory"}
    assert review_issue_result["final_decision"] is None
    assert readiness_result["final_decision"] is None
    assert review_issue_result["advisories"] == readiness_result["advisories"]

    # Blocking-gate verdict on both consumers stays PASS/empty (advisory-only).
    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY, "implementation"
    )
    readiness_blocking_errors = contract_readiness_check.check_extension_surface_risk_trigger(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY
    )
    assert review_issue_status == check_issue_contract.CheckResult.PASS
    assert review_issue_reasons == []
    assert readiness_blocking_errors == []

    # PR #2370 P0: after merge_readiness_into_review_result(), the
    # EXTSURF003 advisory must still not have flipped the merged verdict.
    validate_result = contract_readiness_check.run_validate_issue_body(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY
    )
    readiness_full_result = contract_readiness_check.build_result(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY, "static", validate_result, None, None
    )
    assert readiness_full_result["status"] == "go"

    fixture_path = _REVIEW_ISSUE_SCRIPTS.parent / "fixtures" / "pass_issue.md"
    body, labels, title = check_issue_contract.load_fixture_file(str(fixture_path))
    clean_result = check_issue_contract.result_to_dict(
        check_issue_contract.run_checks(body, labels=labels, title=title, body_file_path=str(fixture_path))
    )
    assert clean_result["verdict"] == "approve"

    merged = check_issue_contract.merge_readiness_into_review_result(
        clean_result,
        {**readiness_full_result, "body_sha256": clean_result["body_sha256"]},
        readiness_artifact_path="test_artifact_path/readiness.json",
        iteration_id="test-parity-matrix-unclassified",
    )
    assert merged["verdict"] == "approve"
    assert merged["blocking_issues"] == []
    assert merged["structured_blockers"] == []
    assert merged["non_blocking_improvements"]


class _StubPolicyUnavailableEvaluator:
    """Stand-in for a freshly-loaded `extension_surface_policy_matcher`
    module whose policy YAML is malformed -- both consumer functions only
    ever touch `evaluator.PolicyLoadError` / `evaluator.evaluate_issue_risk_
    trigger` / `evaluator.evaluate_allowed_paths`, so this minimal stub is
    sufficient to force the "policy unavailable" branch deterministically
    without needing a real broken policy YAML file on disk.
    """

    class PolicyLoadError(Exception):
        pass

    @classmethod
    def evaluate_issue_risk_trigger(cls, **kwargs):
        raise cls.PolicyLoadError("synthetic malformed policy for AC7 parity test")

    @classmethod
    def evaluate_allowed_paths(cls, *args, **kwargs):
        raise cls.PolicyLoadError("synthetic malformed policy for AC7 parity test")

    @classmethod
    def evaluate_runtime_assertion_binding_coverage(cls, **kwargs):
        # Issue #2771: `build_result()` now also calls this shared-evaluator
        # entrypoint unconditionally for every implementation-kind body with
        # a non-empty Allowed Paths set, same as `evaluate_issue_risk_
        # trigger`/`evaluate_allowed_paths` above -- this stub must raise the
        # same PolicyLoadError so the "malformed policy" scenario this test
        # simulates stays internally consistent instead of surfacing an
        # unrelated AttributeError.
        raise cls.PolicyLoadError("synthetic malformed policy for AC7 parity test")


def test_malformed_policy_routing_is_intentionally_different_and_documented(monkeypatch):
    """PR #2370 OWNER review fix_delta (P1, malformed-policy sub-bullet): the
    two consumers intentionally route a `PolicyLoadError` (malformed/
    unavailable policy) differently -- `review-issue`'s C14 downgrades to a
    non-blocking `CheckResult.WARN` (a same-repo/env-level policy defect
    should not hard-block the review loop), while `issue-contract-review`'s
    readiness check escalates to a blocking `human_judgment`
    (`extension_surface_risk_trigger_policy_unavailable`) finding (the
    readiness gate is specifically designed to stop `go` on an environment/
    policy defect before advancing the Issue). This divergence already has
    explicit rationale comments at each call site
    (`check_c14_extension_surface_risk_trigger()` /
    `check_extension_surface_risk_trigger()`); this test makes the
    divergence itself an explicit, asserted contract instead of an untested
    implicit mismatch (Issue #2339 AC7 fix_delta).
    """
    check_issue_contract, contract_readiness_check = _load_consumers("matrix_malformed")

    monkeypatch.setattr(
        check_issue_contract,
        "_load_extension_surface_policy_matcher",
        lambda: _StubPolicyUnavailableEvaluator,
    )
    monkeypatch.setattr(
        contract_readiness_check,
        "_load_extension_surface_policy_matcher",
        lambda: _StubPolicyUnavailableEvaluator,
    )

    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _ORDINARY_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(_ORDINARY_FIXTURE_BODY)

    # Both sides surface SOMETHING (never silently swallowed) ...
    assert review_issue_status == check_issue_contract.CheckResult.WARN
    assert review_issue_reasons and "synthetic malformed policy" in review_issue_reasons[0]
    assert len(readiness_errors) == 1
    assert readiness_errors[0]["category"] == "extension_surface_risk_trigger_policy_unavailable"
    assert readiness_errors[0]["rule_id"] == "EXTSURF002"

    # ... but with intentionally different severities: review-issue's WARN
    # never blocks (never == CheckResult.FAIL), while readiness's
    # EXTSURF002 always escalates `overall_status` to human_judgment.
    assert review_issue_status != check_issue_contract.CheckResult.FAIL
    validate_result = contract_readiness_check.run_validate_issue_body(_ORDINARY_FIXTURE_BODY)
    readiness_full_result = contract_readiness_check.build_result(
        _ORDINARY_FIXTURE_BODY, "static", validate_result, None, None
    )
    assert readiness_full_result["status"] == "human_judgment"
