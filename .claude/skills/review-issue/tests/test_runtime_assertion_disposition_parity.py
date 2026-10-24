"""Issue #2852 AC9 (review-issue side): the non-blocking disposition
classification carrier emitted by `run_checks()` / merged by
`merge_readiness_into_review_result()`.

Pinned here:

- the carrier shape (`code`, `severity`, one verbatim evidence line per binding)
  and its emission condition (explicit disposition / not_applicable / compat
  only; never for a pure legacy 3-field input),
- it is emitted ONLY when C15 passed (never over a FAIL or a policy-unavailable
  WARN primary result) and with `emit_finding=False` (finding count unchanged),
- it never changes the verdict / blocking_issues,
- identity: readiness `category` and review `code` are the same string, and a
  merged result holds the carrier EXACTLY ONCE (the review side emits it
  directly and the readiness side carries the same classification).

The cross-checker parity (review-issue vs issue-contract-review on the same
input) lives in
`.claude/skills/issue-contract-review/tests/test_runtime_assertion_disposition_parity.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
import check_issue_contract as checker  # noqa: E402

_CODE = "runtime_assertion_disposition_classification"
_SKILL = "skill-invocation-runtime-smoke"
_SKILL_A1 = "procedure_steps_executed_in_declared_order"
_SKILL_A2 = "output_contract_schema_fields_present"
_SUBAGENT = "subagent-lifecycle-causal-evidence-smoke"
_SUBAGENT_A1 = "subagent_start_stop_causal_evidence_correlated"

_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "assertion disposition carrier fixture"
change_kind: workflow
```

## Parent Issue

none

## Outcome

`.claude/skills/foo/SKILL.md` の変更に対し runtime_assertion_bindings の分類が公開結果に残る。

## Parent Goal Ref

- Goal: disposition carrier の検証
- Desired Destination: 分類が公開結果に残る

## Current Validated Scope

- `.claude/skills/foo/SKILL.md` を変更する

## Remaining Parent Gaps

なし

## In Scope

- `.claude/skills/foo/SKILL.md` を変更する

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: 手順 AC が実行順を検証する <!-- runtime-verification: true -->
- [ ] AC2: 別の deterministic test が出力契約を検証する

## Verification Commands

```bash
# AC1
$ rg -n "procedure" .claude/skills/foo/SKILL.md

# AC2
$ rg -n "output" .claude/skills/foo/SKILL.md
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
  - AC1
execution_environment:
  cli_tools:
    - rg
skip_conditions:
  - "none"
fallback_policy:
  fallback_success_is_pass: false
artifact_requirements:
  - "artifacts/out.json"
runtime_assertion_bindings:
__BINDINGS__
```

## Allowed Paths

- `.claude/skills/foo/SKILL.md`
"""


def _body(bindings: str) -> str:
    return _BODY_TEMPLATE.replace("__BINDINGS__", bindings.rstrip("\n"))


def _run(body: str) -> dict:
    return checker.result_to_dict(
        checker.run_checks(body, labels="phase/implementation,kind/implementation", title="実装: carrier fixture")
    )


_LEGACY = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n    ac: AC1\n"
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n    ac: AC1\n"
)
_MIXED = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n"
    "    disposition: non_dispositive_readiness_compat\n"
    "    demonstrated_by: AC2\n"
    '    reason: "AC2 が出力契約の実証を所有する"\n'
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n"
    "    disposition: not_applicable\n"
    '    reason: "handoff が production path に存在しない"\n'
)
_MIXED_EVIDENCE = [
    f"{_SKILL}/{_SKILL_A1}: disposition=dispositive; source=legacy_default; demonstrated_by=-; reason=-",
    f"{_SKILL}/{_SKILL_A2}: disposition=non_dispositive_readiness_compat; source=explicit; "
    "demonstrated_by=AC2; reason=AC2 が出力契約の実証を所有する",
    f"{_SUBAGENT}/{_SUBAGENT_A1}: disposition=not_applicable; source=explicit; "
    "demonstrated_by=-; reason=handoff が production path に存在しない",
]


def _carriers(result: dict) -> list[dict]:
    return [e for e in result["non_blocking_improvements"] if e["code"] == _CODE]


def test_mixed_input_emits_exact_one_line_per_binding_carrier_and_stays_approve():
    """GIVEN 3 dispositions + a legacy binding that pass C15
    WHEN review-issue runs
    THEN it approves and emits one carrier whose evidence is one verbatim line per binding."""
    result = _run(_body(_MIXED))
    assert result["verdict"] == "approve", result["blocking_issues"]
    assert result["deterministic_checks"]["C1_required_sections"] == "pass"
    carriers = _carriers(result)
    assert len(carriers) == 1
    assert carriers[0]["severity"] == "advisory"
    assert carriers[0]["evidence"] == _MIXED_EVIDENCE


def test_carrier_does_not_grow_findings_and_leaves_verdict_unchanged_versus_legacy():
    """GIVEN the same body once with a carrier-producing disposition and once pure legacy
    WHEN review-issue runs
    THEN verdict, blocking_issues, structured_blockers and findings (heuristic count) are equal."""
    mixed = _run(_body(_MIXED))
    legacy = _run(_body(_LEGACY))
    assert _carriers(legacy) == []
    for key in ("verdict", "blocking_issues", "structured_blockers", "findings"):
        assert mixed[key] == legacy[key], key
    heuristic = lambda r: [f for f in r["findings"] if f["finding_kind"] == "heuristic_concern"]  # noqa: E731
    assert len(heuristic(mixed)) == len(heuristic(legacy))
    assert mixed["deterministic_checks"] == legacy["deterministic_checks"]


def test_single_explicit_dispositive_binding_triggers_the_carrier_for_all_bindings():
    """GIVEN legacy bindings plus ONE explicit `disposition: dispositive`
    WHEN review-issue runs
    THEN the carrier is emitted for every binding (explicit counts as a trigger)."""
    bindings = _LEGACY.replace(
        f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n    ac: AC1\n",
        f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n    ac: AC1\n    disposition: dispositive\n",
    )
    result = _run(_body(bindings))
    carriers = _carriers(result)
    assert len(carriers) == 1 and len(carriers[0]["evidence"]) == 3
    assert carriers[0]["evidence"][-1].endswith("source=explicit; demonstrated_by=-; reason=-")


def test_pure_legacy_input_does_not_emit_the_carrier():
    """GIVEN a pure legacy 3-field input
    WHEN review-issue runs
    THEN no carrier is emitted (the existing approve output is unchanged)."""
    result = _run(_body(_LEGACY))
    assert result["verdict"] == "approve", result["blocking_issues"]
    assert _carriers(result) == []


def test_carrier_is_not_emitted_over_a_c15_failure():
    """GIVEN not_applicable next to a missing required assertion (C15 FAIL)
    WHEN review-issue runs
    THEN the primary FAIL stands and no carrier decorates it."""
    bindings = (
        f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
        f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n"
        "    disposition: not_applicable\n"
        '    reason: "handoff が存在しない"\n'
    )
    result = _run(_body(bindings))
    assert result["verdict"] == "needs-fix"
    assert any(_SKILL_A2 in issue for issue in result["blocking_issues"])
    assert _carriers(result) == []


def test_carrier_is_not_emitted_over_a_policy_unavailable_warn(monkeypatch):
    """GIVEN the shared evaluator reports a policy integrity failure (C15 WARN)
    WHEN review-issue runs
    THEN no carrier is emitted over that primary result."""

    class _Stub:
        class PolicyLoadError(Exception):
            pass

        @classmethod
        def evaluate_issue_risk_trigger(cls, **kwargs):
            raise cls.PolicyLoadError("synthetic")

        @classmethod
        def evaluate_allowed_paths(cls, *args, **kwargs):
            raise cls.PolicyLoadError("synthetic")

        @classmethod
        def evaluate_runtime_assertion_binding_coverage(cls, **kwargs):
            raise cls.PolicyLoadError("synthetic")

    monkeypatch.setattr(checker, "_load_extension_surface_policy_matcher", lambda: _Stub)
    body = _body(_MIXED)
    status, _issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.WARN
    assert checker.get_runtime_assertion_disposition_carrier(body, "implementation") == []
    assert _carriers(_run(body)) == []


def test_carrier_helper_is_empty_for_non_implementation_kind():
    """GIVEN a non-implementation issue kind
    WHEN the carrier helper is asked
    THEN it returns nothing (C15 is NA)."""
    assert checker.get_runtime_assertion_disposition_carrier(_body(_MIXED), "research") == []


# --- merge identity / exact-count pins (warning A) -------------------------


def _readiness_with_carrier(body_sha256: str, evidence: list[str]) -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "go",
        "body_sha256": body_sha256,
        "source_checks": [],
        "errors": [
            {
                "rule_id": "RUNTIMEASSERT003",
                "severity": "info",
                "source_check": "contract_readiness_check",
                "category": _CODE,
                "section": "Runtime Verification Applicability",
                "line_start": 0,
                "line_end": 0,
                "minimal_context": evidence,
                "fix_hint": "Non-blocking: classification carrier",
                "autofixable": False,
            }
        ],
        "minimal_context": [],
        "fix_hint": None,
    }


def _merge(review_result: dict, readiness_result: dict) -> dict:
    return checker.merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="test_artifact_path/readiness.json",
        iteration_id="test-iteration",
    )


def test_readiness_category_is_registered_as_non_blocking():
    """GIVEN the carrier category
    WHEN the non-blocking category set is read
    THEN the carrier category is in it (and the EXTSURF003 category is retained)."""
    assert _CODE in checker._NON_BLOCKING_READINESS_ERROR_CATEGORIES
    assert "extension_surface_candidate_advisory" in checker._NON_BLOCKING_READINESS_ERROR_CATEGORIES


def test_readiness_only_carrier_is_merged_under_the_category_code_not_the_rule_id():
    """GIVEN a clean review result (no carrier) and a readiness carrier error
    WHEN merged
    THEN exactly one non_blocking entry exists and its code is the category string,
    not RUNTIMEASSERT003; the verdict stays approve."""
    review = _run(_body(_LEGACY))
    assert _carriers(review) == []
    merged = _merge(review, _readiness_with_carrier(review["body_sha256"], _MIXED_EVIDENCE))
    assert merged["verdict"] == "approve"
    assert merged["blocking_issues"] == [] and merged["structured_blockers"] == []
    assert merged.get("failure_class") is None
    codes = [e["code"] for e in merged["non_blocking_improvements"]]
    assert codes == [_CODE]
    assert merged["non_blocking_improvements"][0]["evidence"] == _MIXED_EVIDENCE


def test_review_and_readiness_carrier_with_identical_evidence_appear_exactly_once():
    """GIVEN the review result already holds the carrier (run_checks) and readiness carries the same
    classification
    WHEN merged
    THEN merged_review_result holds exactly ONE carrier entry (no double path)."""
    review = _run(_body(_MIXED))
    assert len(_carriers(review)) == 1
    merged = _merge(review, _readiness_with_carrier(review["body_sha256"], _MIXED_EVIDENCE))
    assert len(_carriers(merged)) == 1
    assert [e["code"] for e in merged["non_blocking_improvements"]].count("RUNTIMEASSERT003") == 0
    assert merged["verdict"] == "approve"


def test_diverging_carrier_evidence_is_not_silently_collapsed():
    """GIVEN the review carrier and a readiness carrier with DIFFERENT evidence
    WHEN merged
    THEN both stay visible (the narrow dedupe only drops byte-identical duplicates)."""
    review = _run(_body(_MIXED))
    merged = _merge(review, _readiness_with_carrier(review["body_sha256"], ["other/assertion: disposition=x"]))
    assert len(_carriers(merged)) == 2


def test_carrier_readiness_error_never_becomes_a_blocker_even_beside_a_real_blocker():
    """GIVEN the carrier plus a genuinely blocking readiness error
    WHEN merged
    THEN only the real error blocks; the carrier is not in blocking_issues."""
    review = _run(_body(_LEGACY))
    readiness = _readiness_with_carrier(review["body_sha256"], _MIXED_EVIDENCE)
    readiness["status"] = "needs_fix"
    readiness["errors"].append(
        {
            "rule_id": "EXTSURF001",
            "severity": "error",
            "source_check": "contract_readiness_check",
            "category": "extension_surface_risk_trigger",
            "section": "Runtime Verification Applicability",
            "line_start": 1,
            "line_end": 1,
            "minimal_context": ["synthetic blocker"],
            "fix_hint": "synthetic blocker fix_hint",
            "autofixable": False,
        }
    )
    merged = _merge(review, readiness)
    assert merged["verdict"] == "needs-fix"
    assert merged["blocking_issues"] == ["synthetic blocker fix_hint"]
    assert [e["code"] for e in merged["non_blocking_improvements"]] == [_CODE]
