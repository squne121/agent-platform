"""
Tests for ISSUE_EXECUTION_DECISION_V1 in plan_refinement_loop.py output (#1677).

Covers:
- AC1: relations[] field present (machine-readable, array)
- AC2: execution.state / execution.predecessors / execution.defer_reason present
- AC3: downstream_policy block present with fixed values
- AC6: execution.state == selected fixture (requires a *complete*
  scope-rollup artifact confirming no collisions -- artifact absence no
  longer fails open to selected, PR #1767 owner review P0-1)
- AC7 / AC8: blocked / duplicate states are validated directly against the
  schema + normative semantic validator (construction bypasses the planner,
  since ISSUE_SCOPE_ROLLUP_PLAN_V2 heuristics are deliberately no longer
  converted into depends_on/duplicate/absorb relations -- PR #1767 owner
  review P0-2). Deriving these states from a genuinely reliable source
  (GitHub native dependency graph, explicit duplicate markers) is tracked as
  follow-up work, not implemented by this module.
- Negative tests (PR #1767 owner review P1-2): scope-rollup artifact
  absence/corruption does not fail open to selected; ambiguous
  sequential_required / merge_into_current_pr / human_review_required
  signals are never silently converted into a fabricated semantic relation.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import plan_refinement_loop as prl  # noqa: E402
import validate_issue_execution_decision as vied  # noqa: E402


REQUIRED_SECTIONS_BODY = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Parent Issue
#1

## Parent Goal Ref
- Goal: test

## Current Validated Scope
- foo

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため

## Outcome
Some outcome.

## In Scope
- foo.py

## Out of Scope
- bar.py

## Remaining Parent Gaps
- none

## Acceptance Criteria
- [ ] AC1: foo

## Verification Commands
```bash
echo hi
```

## Allowed Paths
- foo.py

## Stop Conditions
- none

## Required Skills
- none
"""


def _planner_input(issue_number: int, known_context: dict | None = None) -> dict:
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "test",
            "body": REQUIRED_SECTIONS_BODY,
            "labels": [],
        },
        "comments": [],
        "known_context": known_context,
        "now": "2026-07-25T00:00:00Z",
    }


def _run(issue_number: int, known_context: dict | None = None) -> dict:
    plan, exit_code = prl.plan_refinement_loop(_planner_input(issue_number, known_context))
    assert exit_code == 0, plan
    assert plan["fail_closed"]["required"] is False, plan["fail_closed"]
    return plan


def _complete_scope_rollup(candidates: list | None = None) -> dict:
    return {
        "schema_version": 2,
        "input": {"completeness": "full", "warnings": []},
        "candidates": candidates or [],
    }


def test_relations_field_present():
    """AC1: ISSUE_EXECUTION_DECISION_V1.relations is a machine-readable array."""
    plan = _run(1001, known_context={"scope_rollup_result": _complete_scope_rollup()})
    decision = plan["issue_execution_decision"]
    assert isinstance(decision["relations"], list)
    assert decision["schema_version"] == "ISSUE_EXECUTION_DECISION_V1"


def test_execution_state_fields_present():
    """AC2: execution.state / predecessors / defer_reason are present."""
    plan = _run(1002, known_context={"scope_rollup_result": _complete_scope_rollup()})
    execution = plan["issue_execution_decision"]["execution"]
    assert execution["state"] in ("selected", "deferred", "blocked", "duplicate")
    assert isinstance(execution["predecessors"], list)
    assert "defer_reason" in execution


def test_downstream_policy_present():
    """AC3: downstream_policy block with fixed values is present."""
    plan = _run(1003, known_context={"scope_rollup_result": _complete_scope_rollup()})
    policy = plan["issue_execution_decision"]["downstream_policy"]
    assert policy == {
        "semantic_reclassification": "forbidden",
        "freshness_validation": "required",
        "stale_action": "rerun_issue_refinement",
    }


def test_execution_state_selected_fixture():
    """AC6: a complete scope-rollup artifact with no collision candidates -> selected."""
    plan = _run(1004, known_context={"scope_rollup_result": _complete_scope_rollup()})
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] == "selected"
    assert decision["execution"]["predecessors"] == []
    assert decision["execution"]["defer_reason"] is None
    assert vied.validate_issue_execution_decision(decision) == []


def test_execution_state_blocked_fixture():
    """
    AC7: 'blocked' with non-empty predecessors is a valid, schema+semantic-
    conformant ISSUE_EXECUTION_DECISION_V1 shape.

    Constructed directly (not derived by the planner from scope-rollup
    heuristics -- see module docstring) to prove the contract itself
    correctly models this state, independent of which upstream source will
    eventually populate it (follow-up: GitHub native depends_on graph).
    """
    body_sha = "sha256:" + "b" * 64
    decision = {
        "schema_version": "ISSUE_EXECUTION_DECISION_V1",
        "identity": {
            "target_issue_number": 1007,
            "target_body_sha256": body_sha,
            "generated_at": "2026-07-25T00:00:00Z",
            "collection_digest": "sha256:" + "0" * 64,
        },
        "nodes": [
            {"issue_number": 1007, "body_sha256": body_sha},
            {"issue_number": 9001, "body_sha256": "sha256:" + "c" * 64},
        ],
        "relations": [
            {
                "source_issue_number": 1007,
                "target_issue_number": 9001,
                "relation_type": "depends_on",
                "evidence": ["native_dependency:blockedBy:9001"],
            }
        ],
        "execution": {
            "state": "blocked",
            "target_issue_number": 1007,
            "predecessors": [9001],
            "defer_reason": "open predecessor issue(s) pending: #9001",
        },
        "downstream_policy": dict(prl.ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY),
        "completeness": {
            "issues_complete": True,
            "dependencies_complete": True,
            "unresolved_references": [],
        },
    }
    decision["identity"]["collection_digest"] = prl._sha256_prefixed(
        prl._canonical_json(
            {
                "nodes": decision["nodes"],
                "relations": decision["relations"],
                "execution": decision["execution"],
                "completeness": decision["completeness"],
                "downstream_policy": decision["downstream_policy"],
            }
        )
    )
    assert vied.validate_issue_execution_decision(decision) == []


def test_relations_duplicate_fixture():
    """
    AC8: relations containing relation_type 'duplicate' with execution.state
    == duplicate is a valid, schema+semantic-conformant shape.

    Constructed directly for the same reason as the blocked fixture above --
    ISSUE_SCOPE_ROLLUP_PLAN_V2's suggested_action heuristics are no longer
    auto-converted into 'duplicate' (PR #1767 owner review P0-2); an
    authoritative source (explicit duplicate marker / GitHub duplicate
    event) is required in production.
    """
    body_sha = "sha256:" + "d" * 64
    decision = {
        "schema_version": "ISSUE_EXECUTION_DECISION_V1",
        "identity": {
            "target_issue_number": 1008,
            "target_body_sha256": body_sha,
            "generated_at": "2026-07-25T00:00:00Z",
            "collection_digest": "sha256:" + "0" * 64,
        },
        "nodes": [
            {"issue_number": 1008, "body_sha256": body_sha},
            {"issue_number": 9002, "body_sha256": "sha256:" + "e" * 64},
        ],
        "relations": [
            {
                "source_issue_number": 1008,
                "target_issue_number": 9002,
                "relation_type": "duplicate",
                "evidence": ["explicit_duplicate_marker:#9002"],
            }
        ],
        "execution": {
            "state": "duplicate",
            "target_issue_number": 1008,
            "predecessors": [],
            "defer_reason": "duplicate or absorb relation confirmed by an authoritative source",
        },
        "downstream_policy": dict(prl.ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY),
        "completeness": {
            "issues_complete": True,
            "dependencies_complete": True,
            "unresolved_references": [],
        },
    }
    decision["identity"]["collection_digest"] = prl._sha256_prefixed(
        prl._canonical_json(
            {
                "nodes": decision["nodes"],
                "relations": decision["relations"],
                "execution": decision["execution"],
                "completeness": decision["completeness"],
                "downstream_policy": decision["downstream_policy"],
            }
        )
    )
    relation_types = {r["relation_type"] for r in decision["relations"]}
    assert "duplicate" in relation_types
    assert decision["execution"]["state"] == "duplicate"
    assert vied.validate_issue_execution_decision(decision) == []


# ---------------------------------------------------------------------------
# Negative tests (PR #1767 owner review P0-1 / P0-2 / P1-2)
# ---------------------------------------------------------------------------


def test_missing_scope_rollup_artifact_does_not_fail_open_to_selected():
    """
    P0-1 fix: absence of a scope-rollup artifact must NOT resolve to
    'selected'. It is unresolved evidence, not a proof of no collision.
    """
    plan = _run(1009, known_context=None)
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] != "selected"
    assert decision["completeness"]["issues_complete"] is False
    assert vied.validate_issue_execution_decision(decision) == []


def test_wrong_schema_version_artifact_does_not_fail_open_to_selected():
    """P0-1 fix: an artifact with an unexpected schema_version is incomplete evidence."""
    scope_rollup = _complete_scope_rollup()
    scope_rollup["schema_version"] = 1
    plan = _run(1010, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] != "selected"
    assert decision["completeness"]["issues_complete"] is False


def test_missing_completeness_field_does_not_fail_open_to_selected():
    """P0-1 fix: missing/unknown input.completeness is incomplete evidence, not 'full'."""
    scope_rollup = _complete_scope_rollup()
    del scope_rollup["input"]["completeness"]
    plan = _run(1011, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] != "selected"
    assert decision["completeness"]["issues_complete"] is False


def test_malformed_candidate_does_not_fail_open_to_selected():
    """P0-1 fix: a malformed candidate entry marks evidence incomplete, not silently skipped."""
    scope_rollup = _complete_scope_rollup(candidates=[{"unexpected": "shape"}])
    plan = _run(1012, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] != "selected"
    assert decision["completeness"]["issues_complete"] is False


def test_sequential_required_is_not_converted_into_directed_predecessor():
    """
    P0-2 fix: ordering_constraint=sequential_required means direction is
    undetermined (human judgment required) -- it must never become a
    depends_on relation asserting the candidate as a predecessor.
    """
    scope_rollup = _complete_scope_rollup(
        candidates=[
            {
                "kind": "issue",
                "number": 9001,
                "state": "OPEN",
                "signals": ["same_parent_issue"],
                "suggested_action": "keep_separate_with_reason",
                "ordering_constraint": "sequential_required",
            }
        ]
    )
    plan = _run(1013, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    relation_types = {r["relation_type"] for r in decision["relations"]}
    assert "depends_on" not in relation_types
    assert decision["execution"]["state"] not in ("blocked", "selected", "duplicate")
    assert decision["execution"]["predecessors"] == []
    assert vied.validate_issue_execution_decision(decision) == []


def test_merge_into_current_pr_is_not_converted_into_issue_duplicate():
    """
    P0-2 fix: suggested_action=merge_into_current_pr describes coordinating
    work, not a confirmed Issue-duplicate relation.
    """
    scope_rollup = _complete_scope_rollup(
        candidates=[
            {
                "kind": "issue",
                "number": 9002,
                "state": "OPEN",
                "signals": ["shared_dedupe_key"],
                "suggested_action": "merge_into_current_pr",
                "ordering_constraint": "parallel_ok",
            }
        ]
    )
    plan = _run(1014, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    relation_types = {r["relation_type"] for r in decision["relations"]}
    assert "duplicate" not in relation_types
    assert decision["execution"]["state"] != "duplicate"
    assert decision["execution"]["state"] != "selected"


def test_human_review_required_candidate_does_not_resolve_to_selected():
    """
    P0-2 fix: a candidate the scope-rollup planner itself flagged as
    human_review_required must not silently disappear into 'selected'.
    """
    scope_rollup = _complete_scope_rollup(
        candidates=[
            {
                "kind": "issue",
                "number": 9003,
                "state": "OPEN",
                "signals": ["allowed_path_intersection"],
                "suggested_action": "human_review_required",
                "ordering_constraint": "parallel_ok",
            }
        ]
    )
    plan = _run(1015, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] != "selected"


def test_pr_candidate_does_not_collide_with_issue_node():
    """
    P0-2 fix: a PR candidate sharing a number with the target/other Issue
    nodes must not be folded into the same issue_number node.
    """
    scope_rollup = _complete_scope_rollup(
        candidates=[
            {
                "kind": "pr",
                "number": 1016,
                "state": "OPEN",
                "signals": ["shared_dedupe_key"],
                "suggested_action": "merge_into_current_pr",
                "ordering_constraint": "parallel_ok",
            }
        ]
    )
    plan = _run(1016, known_context={"scope_rollup_result": scope_rollup})
    decision = plan["issue_execution_decision"]
    node_numbers = [n["issue_number"] for n in decision["nodes"]]
    # Only the target Issue itself should be a node; the PR candidate must
    # not be added as an issue_number node under the same number.
    assert node_numbers == [1016]
    assert decision["execution"]["state"] == "selected"


def test_target_body_sha256_is_not_double_hashed():
    """P0-3.1 regression: identity.target_body_sha256 must be sha256:<hex(body)>,
    not sha256:<hex(hex(body))>."""
    plan = _run(1017, known_context={"scope_rollup_result": _complete_scope_rollup()})
    decision = plan["issue_execution_decision"]
    expected = "sha256:" + prl._sha256(REQUIRED_SECTIONS_BODY)
    assert decision["identity"]["target_body_sha256"] == expected


def test_stale_collection_digest_is_rejected():
    """
    P0-3.4 regression (#1873: plan_refinement_loop.py no longer
    self-validates/self-repairs its own issue_execution_decision output --
    that caller-side fallback was removed along with the Replay Arbitration
    self-check; downstream consumers like run_refinement_preflight.py now
    independently validate it). This test directly exercises the canonical
    validator with a deliberately stale collection_digest to prove it is
    still rejected.
    """
    body_sha = "sha256:" + "f" * 64
    decision = {
        "schema_version": "ISSUE_EXECUTION_DECISION_V1",
        "identity": {
            "target_issue_number": 1018,
            "target_body_sha256": body_sha,
            "generated_at": "2026-07-25T00:00:00Z",
            "collection_digest": "sha256:" + "0" * 64,  # stale/wrong on purpose
        },
        "nodes": [{"issue_number": 1018, "body_sha256": body_sha}],
        "relations": [],
        "execution": {
            "state": "deferred",
            "target_issue_number": 1018,
            "predecessors": [],
            "defer_reason": "forced fallback",
        },
        "downstream_policy": dict(prl.ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY),
        "completeness": {
            "issues_complete": False,
            "dependencies_complete": False,
            "unresolved_references": [1018],
        },
    }
    violations = vied.validate_issue_execution_decision(decision)
    assert "collection_digest_mismatch" in violations
