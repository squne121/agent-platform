from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

router = importlib.import_module("decide_next_loop_action")


def _base_loop_state(**overrides) -> dict:
    state = {
        "schema_version": "loop_state/v1",
        "issue_number": 1323,
        "iteration": 0,
        "max_iterations": 3,
        "last_verdict": None,
        "termination_reason": None,
        "scope_signal_guard": {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_in_scope_area",
        },
        "delivery_rollup": {"applicable": False, "unmaterialized_slots": []},
        "follow_up_materialization": {"candidates": []},
        "web_research_policy": {
            "required": False,
            "reason": None,
            "critical_external_claims": [],
            "skip_reason": None,
        },
    }
    state.update(overrides)
    return state


def _contract_update_decision_v2(*, authority_route_action: str = "contract_update_required") -> dict:
    """Build a SCOPE_SIGNAL_GUARD_DECISION_V2 sidecar with a NESTED
    scope_delta_authority.route.action (PR #1332 review fix, P0).

    Unlike the pre-fix version of this test module, this helper NEVER sets
    the top-level "route" key to "contract_update_required" -- that
    top-level field is a *different*, pre-existing (#1090) enum
    (not_triggered / human_judgment_required / invalid_scope_delta_approval /
    proceed_with_notes) used for the ANCHOR_SCOPE_REFRAME_V1 lane split, and
    it never takes the value "contract_update_required". The router must
    read scope_delta_authority.route.action instead.
    """
    return {
        "schema_version": "SCOPE_SIGNAL_GUARD_DECISION_V2",
        "raw_signal": {"triggered": True, "reason_code": "new_in_scope_area"},
        "scope_context": {"path_layer": ["skill"]},
        "scope_delta_approval": {
            "present": False,
            "valid": False,
            "status": "not_required",
            "missing_approval_field": False,
            "suggested_contract_patch": None,
            "comment_id": None,
            "comment_url": None,
            "body_sha256": None,
            "author_association": None,
            "created_at": None,
            "issue_url": None,
            "required_rerun": [],
        },
        "security_sensitive": False,
        # NOTE: top-level route intentionally stays "not_triggered" (its own
        # #1090 enum) -- the contract-update signal lives only in the nested
        # scope_delta_authority.route.action below (AC20 regression guard).
        "route": "not_triggered",
        "scope_delta_authority": {
            "schema_version": "SCOPE_DELTA_AUTHORITY_V1",
            "authority_category": "human_review_directive",
            "provenance": {
                "source_kind": "issue_comment",
                "source_ref": "https://github.com/squne121/agent-platform/issues/1323#issuecomment-1",
                "body_sha256": "sha256:deadbeef",
                "author_association": "OWNER",
            },
            "directive": {"confidence": "explicit", "extracted_markers": ["revised acceptance criteria"]},
            "boundary_flags": {
                "expands_allowed_paths": False,
                "changes_permission_boundary": False,
                "changes_external_service_boundary": False,
                "destructive_or_non_idempotent_operation": False,
                "requires_issue_split": False,
            },
            "route": {
                "action": authority_route_action,
                "reason_code": "explicit_human_contract_directive"
                if authority_route_action == "contract_update_required"
                else None,
                "implementation_allowed": authority_route_action == "not_triggered",
                "next_step": "rerun_refinement_after_contract_update"
                if authority_route_action == "contract_update_required"
                else None,
            },
        },
    }


# --- AC20 (unit level): decide_next_action() reads the NESTED route --------
# (regression guard: does NOT rely on manually setting the top-level
# scope_signal_guard_decision_v2["route"] key -- see test below asserting
# that mutating top-level route alone has no effect.)


def test_contract_update_required_route_yields_proceed_with_contract_update():
    decision_v2 = _contract_update_decision_v2(authority_route_action="contract_update_required")

    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(),
        review_verdict=None,
        scope_signal_guard_decision_v2=decision_v2,
    )

    assert next_action == router.ACTION_PROCEED_WITH_CONTRACT_UPDATE
    assert status == router.STATUS_PASS
    assert termination_cause_hint is None


def test_contract_update_required_does_not_set_termination_reason():
    # decide_next_action is read-only w.r.t. loop_state; assert the input
    # dict itself is untouched (still termination_reason: None) and that the
    # router's own return tuple carries no termination_reason value at all.
    loop_state = _base_loop_state()
    decision_v2 = _contract_update_decision_v2(authority_route_action="contract_update_required")

    router.decide_next_action(
        loop_state=loop_state,
        review_verdict=None,
        scope_signal_guard_decision_v2=decision_v2,
    )

    assert loop_state["termination_reason"] is None


def test_authority_route_other_than_contract_update_required_falls_through_to_existing_logic():
    decision_v2 = _contract_update_decision_v2(authority_route_action="not_triggered")

    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(
            scope_signal_guard={
                "triggered": False,
                "excluded_by_anchor_reframe": False,
                "reason_code": None,
            }
        ),
        review_verdict="approve",
        scope_signal_guard_decision_v2=decision_v2,
    )
    assert next_action == router.ACTION_PROCEED_TO_STEP_4_5


def test_top_level_route_alone_does_not_trigger_contract_update_regression_guard():
    """PR #1332 review regression guard (P0): manually setting ONLY the
    top-level scope_signal_guard_decision_v2["route"] to
    "contract_update_required" -- WITHOUT the nested
    scope_delta_authority.route.action also being contract_update_required --
    must NOT yield proceed_with_contract_update. This is exactly the
    producer/router hierarchy mismatch flagged in PR #1332 review: the
    top-level route field is a different (#1090) enum and the router must
    never read it for this decision.
    """
    decision_v2 = _contract_update_decision_v2(authority_route_action="human_escalation")
    # Adversarial: force the (irrelevant, different-schema) top-level route
    # to the string "contract_update_required" anyway.
    decision_v2["route"] = "contract_update_required"

    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(),
        review_verdict=None,
        scope_signal_guard_decision_v2=decision_v2,
    )

    assert next_action != router.ACTION_PROCEED_WITH_CONTRACT_UPDATE
    # Falls through to the scope_signal_guard hard stop (loop_state.scope_signal_guard
    # is still triggered=True / not excluded in _base_loop_state()).
    assert next_action == router.ACTION_HUMAN_ESCALATION
    assert termination_cause_hint == "human_judgment_required"


def test_missing_scope_delta_authority_key_does_not_regress_existing_behavior():
    # scope_signal_guard_decision_v2 present but WITHOUT a scope_delta_authority
    # key at all (e.g. an older #1090-only sidecar): existing hard-stop
    # behavior for a triggered, non-excluded scope_signal_guard must still
    # apply unchanged.
    decision_v2 = _contract_update_decision_v2(authority_route_action="contract_update_required")
    del decision_v2["scope_delta_authority"]

    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(),
        review_verdict=None,
        scope_signal_guard_decision_v2=decision_v2,
    )
    assert next_action == router.ACTION_HUMAN_ESCALATION
    assert termination_cause_hint == "human_judgment_required"


def test_missing_scope_signal_guard_decision_v2_does_not_regress_existing_behavior():
    # No sidecar provided at all (None): existing hard-stop behavior for a
    # triggered, non-excluded scope_signal_guard must still apply unchanged.
    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(),
        review_verdict=None,
        scope_signal_guard_decision_v2=None,
    )
    assert next_action == router.ACTION_HUMAN_ESCALATION
    assert termination_cause_hint == "human_judgment_required"


# --- AC20 (end-to-end CLI level) --------------------------------------------


def _run_cli(loop_state: dict, decision_v2: "dict | None" = None) -> subprocess.CompletedProcess:
    argv = [
        sys.executable,
        str(SCRIPTS_DIR / "decide_next_loop_action.py"),
        "--loop-state-json",
        json.dumps(loop_state, ensure_ascii=False),
    ]
    if decision_v2 is not None:
        argv += ["--scope-signal-guard-decision-v2-json", json.dumps(decision_v2, ensure_ascii=False)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_cli_end_to_end_contract_update_required():
    decision_v2 = _contract_update_decision_v2(authority_route_action="contract_update_required")

    result = _run_cli(_base_loop_state(), decision_v2)
    assert result.returncode == 0
    assert f"NEXT_ACTION: {router.ACTION_PROCEED_WITH_CONTRACT_UPDATE}" in result.stdout
    assert "TERMINATION_CAUSE:" not in result.stdout


def test_cli_end_to_end_without_sidecar_regresses_to_existing_escalation():
    result = _run_cli(_base_loop_state())
    assert result.returncode == router.EXIT_HUMAN_ESCALATION
    assert f"NEXT_ACTION: {router.ACTION_HUMAN_ESCALATION}" in result.stdout


# --- termination_reason enum must remain unchanged (Non-Goal / Stop Condition) ---


def test_scope_signal_guard_decision_v2_is_out_of_band_not_loop_state_field():
    """#1873: schemas/loop_state.schema.json was removed along with
    build_loop_state.py, so this is no longer a schema-property assertion --
    it directly asserts that decide_next_action() never reads
    scope_signal_guard_decision_v2 from loop_state itself (the sidecar is
    intentionally passed out-of-band via the separate
    scope_signal_guard_decision_v2 function argument, never through
    loop_state)."""
    state = _base_loop_state(
        scope_signal_guard={
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
    )
    state["scope_signal_guard_decision_v2"] = {"route": "not_triggered"}
    status, next_action, _commands, _blockers, _hint = router.decide_next_action(
        loop_state=state,
        review_verdict="approve",
        scope_signal_guard_decision_v2=None,
    )
    # An unrecognized top-level field embedded in loop_state must not
    # silently be promoted to routing significance -- approve still routes
    # normally since decide_next_action() only reads the sidecar from its
    # own separate argument, never from loop_state.
    assert next_action == "proceed_to_step_4_5"
    assert status == "pass"


# ---------------------------------------------------------------------------
# Producer -> router E2E (PR #1332 review, P0 items 1 & 2):
# plan_refinement_loop() is invoked for real (no fixture hand-injection of
# scope_signal_guard_decision_v2 or its route fields at all) and its actual
# scope_signal_guard_decision_v2 output is fed as a sidecar into
# decide_next_loop_action.py's CLI. This is the only test in this module
# that proves the producer (plan_refinement_loop.py /
# classify_scope_delta_authority()) and the router (decide_next_loop_action.py)
# actually agree on where route lives.
# ---------------------------------------------------------------------------


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _planner_module():
    if "scope_signal_delta" not in sys.modules:
        _load_module("scope_signal_delta", "scope_signal_delta.py")
    return _load_module("plan_refinement_loop_e2e_1332", "plan_refinement_loop.py")


_ISSUE_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: code
```

## Parent Issue

none

## Parent Goal Ref

- Goal: test
- Desired Destination: test

## Current Validated Scope

- docs/foo.md

## Remaining Parent Gaps

none

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため

## Outcome

Test outcome.

## In Scope

- test scope

## Out of Scope

- N/A

## Allowed Paths

- `docs/foo.md`

## Acceptance Criteria

- [ ] AC1: something concrete happens

## Verification Commands

```bash
$ true
```

## Stop Conditions

- N/A

## Required Skills

- none
"""


def _e2e_planner_input(issue_number: int) -> dict[str, Any]:
    scope_delta_authority_evidence = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": "issue_comment",
        "source_ref": f"https://github.com/squne121/agent-platform/issues/{issue_number}#issuecomment-4881420705",
        "source_issue_number": issue_number,
        "comment_id": 4881420705,
        "comment_url": f"https://github.com/squne121/agent-platform/issues/{issue_number}#issuecomment-4881420705",
        "issue_url": f"https://github.com/squne121/agent-platform/issues/{issue_number}",
        "body_sha256": "sha256:35f0c1fa52e29f0f6d6cc2ffb7b83f7781bae29992e512166d442035d1bf6cb6",
        "author_login": "squne121",
        "author_type": "User",
        "author_association": "OWNER",
        "captured_at": "2026-07-04T09:11:40Z",
        "directive_markers": ["revised acceptance criteria"],
        "extracted_directives": ["AC0: provider_auto_policy_v1 must be documented"],
        "ambiguity_flags": [],
        "boundary_flags": [],
        "confidence": "explicit",
    }
    delta_input = {
        "before_body": _ISSUE_BODY_TEMPLATE,
        "current_body": _ISSUE_BODY_TEMPLATE.replace(
            "- `docs/foo.md`", "- `docs/foo.md`\n- `scripts/new_module.py`"
        ),
        "after_body": _ISSUE_BODY_TEMPLATE.replace(
            "- `docs/foo.md`", "- `docs/foo.md`\n- `scripts/new_module.py`"
        ),
        "source_refs": {"before": None, "current": None, "after": None},
    }
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "Test",
            "body": _ISSUE_BODY_TEMPLATE,
            "labels": [],
            "html_url": f"https://github.com/squne121/agent-platform/issues/{issue_number}",
        },
        "comments": None,
        "known_context": {
            "scope_signal_delta_input": delta_input,
            "scope_delta_authority_evidence": [scope_delta_authority_evidence],
        },
        "now": "2026-07-04T12:00:00+00:00",
    }


def test_producer_to_router_e2e_yields_proceed_with_contract_update():
    """Item 1/2 of the PR #1332 review's required tests: call
    plan_refinement_loop() for real, take ITS OWN
    scope_signal_guard_decision_v2 output (no hand injection of any route
    field), pass it as a sidecar to decide_next_loop_action.py's CLI, and
    assert NEXT_ACTION: proceed_with_contract_update.
    """
    planner = _planner_module()
    input_data = _e2e_planner_input(1323)
    plan, exit_code = planner.plan_refinement_loop(input_data)
    assert exit_code == 0

    decision_v2 = plan["scope_signal_guard_decision_v2"]
    # Regression guard: the producer does NOT project a top-level
    # "contract_update_required" route (that would collide with the
    # pre-existing #1090 top-level route enum) -- the signal lives only in
    # the nested scope_delta_authority.route.action.
    assert decision_v2["route"] != "contract_update_required"
    assert decision_v2["scope_delta_authority"]["route"]["action"] == "contract_update_required"

    loop_state = _base_loop_state(issue_number=1323)
    result = _run_cli(loop_state, decision_v2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"NEXT_ACTION: {router.ACTION_PROCEED_WITH_CONTRACT_UPDATE}" in result.stdout
    assert "TERMINATION_CAUSE:" not in result.stdout


def test_producer_to_router_e2e_without_evidence_regresses_to_human_escalation():
    """Same producer -> router path, but WITHOUT scope_delta_authority_evidence
    in known_context: plan_refinement_loop() then never adds
    scope_delta_authority to its output at all, and the router must fall
    through to the pre-existing scope_signal_guard hard stop."""
    planner = _planner_module()
    input_data = _e2e_planner_input(1323)
    del input_data["known_context"]["scope_delta_authority_evidence"]
    plan, exit_code = planner.plan_refinement_loop(input_data)
    assert exit_code == 0

    decision_v2 = plan["scope_signal_guard_decision_v2"]
    assert "scope_delta_authority" not in decision_v2

    loop_state = _base_loop_state(issue_number=1323)
    result = _run_cli(loop_state, decision_v2)
    assert result.returncode == router.EXIT_HUMAN_ESCALATION
    assert f"NEXT_ACTION: {router.ACTION_HUMAN_ESCALATION}" in result.stdout
