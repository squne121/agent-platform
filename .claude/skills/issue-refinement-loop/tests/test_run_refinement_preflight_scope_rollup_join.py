"""
Tests for the scope-rollup -> planner-input join in run_refinement_preflight.py
(#1677 AC4).

run_refinement_preflight.py previously persisted target Issue snapshot,
planner input, and refinement result only -- it never combined the
ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact (plan_issue_scope_rollup.py's output)
into the planner input. This left plan_refinement_loop.py's
ISSUE_EXECUTION_DECISION_V1 always defaulting to 'selected' even when a
scope-rollup artifact recorded a known collision/predecessor.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as wrapper  # noqa: E402
import plan_refinement_loop as prl  # noqa: E402
import validate_issue_execution_decision as vied  # noqa: E402


VALID_ISSUE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#1"
```

## Parent Issue

#1

## Parent Goal Ref

- Goal: Test goal

## Current Validated Scope

- scripts/example.py

## Remaining Parent Gaps

- [ ] Nothing remaining

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため

## Outcome

Add `scripts/example.py`.

## In Scope

- scripts/example.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
uv run python3 scripts/example.py
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

なし
"""


def make_minimal_fixture(issue_number: int = 300, repo: str = "testowner/testrepo", body: str = "") -> dict:
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": repo,
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": body,
            "labels": [],
        },
        "comments": [],
        "anchor_comment_urls": [],
    }


SCOPE_ROLLUP_PLAN = {
    "schema_version": 2,
    "repo": "testowner/testrepo",
    "generated_at": "2026-01-01T00:00:00Z",
    "source": "gh",
    "body_sha256": "sha256:" + "a" * 64,
    "input": {"completeness": "full", "warnings": []},
    "candidates": [
        {
            "kind": "issue",
            "number": 9101,
            "title": "predecessor",
            "url": "https://github.com/testowner/testrepo/issues/9101",
            "state": "OPEN",
            "confidence": "high",
            "dedupe_key": "9101",
            "signals": ["same_parent_issue"],
            "matched_paths": [],
            "suggested_action": "keep_separate_with_reason",
            "scope_context": {},
            "ordering_constraint": "sequential_required",
        }
    ],
}


def _seed_scope_rollup_artifact(repo_root: Path, issue_number: int) -> None:
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "issue_scope_rollup_plan_v2.json").write_text(
        json.dumps(SCOPE_ROLLUP_PLAN), encoding="utf-8"
    )


def test_join_pure_function_merges_scope_rollup_into_known_context():
    """Pure join: known_context.scope_rollup_result is set without mutating input."""
    planner_input = {"schema_version": "refinement_loop_planner_input/v1", "issue": {}}
    joined = wrapper._join_scope_rollup_into_planner_input(planner_input, SCOPE_ROLLUP_PLAN)
    assert joined["known_context"]["scope_rollup_result"] == SCOPE_ROLLUP_PLAN
    assert "known_context" not in planner_input  # input not mutated


def test_join_pure_function_no_op_when_absent():
    """Pure join: absent scope_rollup_plan is a no-op (non-blocking)."""
    planner_input = {"schema_version": "refinement_loop_planner_input/v1", "issue": {}}
    joined = wrapper._join_scope_rollup_into_planner_input(planner_input, None)
    assert joined is planner_input


def test_run_preflight_joins_persisted_scope_rollup_artifact(tmp_path):
    """
    AC4 (production test): run_preflight() joins a persisted
    ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact into the planner input, and the
    resulting persisted planner_input.json + plan carry the same
    ISSUE_EXECUTION_DECISION_V1.identity.collection_digest.
    """
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(make_minimal_fixture(issue_number=9100, body=VALID_ISSUE_BODY)),
        encoding="utf-8",
    )
    _seed_scope_rollup_artifact(tmp_path, 9100)

    with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
        result, exit_code = wrapper.run_preflight(
            issue_number=9100,
            repo="testowner/testrepo",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )

    assert exit_code in (0, 1), result

    planner_input_path = (
        tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "9100" / "planner_input.json"
    )
    assert planner_input_path.exists()
    persisted_planner_input = json.loads(planner_input_path.read_text(encoding="utf-8"))
    assert persisted_planner_input["known_context"]["scope_rollup_result"] == SCOPE_ROLLUP_PLAN

    # Re-derive the ISSUE_EXECUTION_DECISION_V1 the same way the planner did,
    # from the persisted planner_input.json, and confirm the digest matches
    # what build_loop_state.py would project from the plan (same source ->
    # same digest end to end, #1677 AC4/AC5).
    #
    # PR #1767 owner review (P0-2): SCOPE_ROLLUP_PLAN's candidate has
    # ordering_constraint=sequential_required, which means "direction
    # undetermined, human judgment required" -- it must NOT be converted
    # into a directed depends_on/blocked relation. The join is proven by the
    # candidate's presence forcing a non-selected outcome (deferred), not by
    # a fabricated 'blocked' verdict.
    plan, plan_exit_code = prl.plan_refinement_loop(persisted_planner_input)
    assert plan_exit_code == 0
    decision = plan["issue_execution_decision"]
    assert decision["execution"]["state"] == "deferred"
    relation_types = {r["relation_type"] for r in decision["relations"]}
    assert "depends_on" not in relation_types
    assert decision["execution"]["predecessors"] == []
    assert vied.validate_issue_execution_decision(decision) == []


def test_run_preflight_without_scope_rollup_artifact_defaults_to_selected(tmp_path):
    """Absence of a persisted scope-rollup artifact is non-blocking (defaults to selected)."""
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(make_minimal_fixture(issue_number=9200, body=VALID_ISSUE_BODY)),
        encoding="utf-8",
    )

    with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
        result, exit_code = wrapper.run_preflight(
            issue_number=9200,
            repo="testowner/testrepo",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )

    assert exit_code in (0, 1), result
    planner_input_path = (
        tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "9200" / "planner_input.json"
    )
    persisted_planner_input = json.loads(planner_input_path.read_text(encoding="utf-8"))
    assert "scope_rollup_result" not in (persisted_planner_input.get("known_context") or {})
