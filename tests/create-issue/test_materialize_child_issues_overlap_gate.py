"""AC8: overlap preflight gate. When the overlap helper (#948) is not yet available, every
created child must declare #948 as a dependency; when overlap cannot be determined, the run
escalates to a human instead of mutating GitHub.
"""

from __future__ import annotations

import materialize_child_issues as m
import pytest


def _ok_validate(body, kind, title):
    return m.RunResult(0, '{"status": "pass"}')


def _runners(create_calls):
    def create(**kw):
        create_calls.append(kw)
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    return m.Runners(validate=_ok_validate, create=create, gh=lambda a: m.RunResult(0, ""))


def test_overlap_clear_with_provenance_proceeds(valid_plan, clear_overlap):
    valid_plan["overlap"] = clear_overlap
    calls = []
    res = m.materialize(valid_plan, _runners(calls))
    assert res["status"] == "ok"
    assert len(calls) == 1


def test_bare_clear_without_provenance_fails(valid_plan):
    # High 2: status=clear without preflight provenance is fail-closed at schema validation.
    valid_plan["overlap"] = {"status": "clear"}
    with pytest.raises(
        m.PlanValidationError, match="provenance|source|verdict|input_sha256|checked_at"
    ):
        m.validate_plan(valid_plan)


def test_deferred_requires_948_dependency_escalates_when_missing(valid_plan):
    valid_plan["overlap"] = {"status": "deferred_to_issue", "depends_on_issue": 948}
    valid_plan["children"][0]["depends_on"] = []  # missing #948
    calls = []
    res = m.materialize(valid_plan, _runners(calls))
    assert res["status"] == "human_escalation"
    assert calls == []  # no creation when the gate escalates
    assert any("948" in e["reason"] for e in res["escalation_items"])


def test_deferred_with_948_dependency_proceeds(valid_plan):
    valid_plan["overlap"] = {"status": "deferred_to_issue", "depends_on_issue": 948}
    valid_plan["children"][0]["depends_on"] = [948]
    calls = []
    res = m.materialize(valid_plan, _runners(calls))
    assert res["status"] == "ok"
    assert len(calls) == 1


def test_not_run_default_requires_948(valid_plan):
    # No overlap key at all → treated as not_run → must declare the default overlap issue.
    valid_plan.pop("overlap", None)
    valid_plan["children"][0]["depends_on"] = []
    calls = []
    res = m.materialize(valid_plan, _runners(calls))
    assert res["status"] == "human_escalation"
    assert calls == []


def test_undeterminable_escalates(valid_plan):
    valid_plan["overlap"] = {"status": "undeterminable", "reason": "paths unparseable"}
    valid_plan["children"][0]["depends_on"] = [948]  # even with dep, undeterminable escalates
    calls = []
    res = m.materialize(valid_plan, _runners(calls))
    assert res["status"] == "human_escalation"
    assert calls == []


def test_gate_unit_clear():
    gate = m.evaluate_overlap_gate({"children": [], "overlap": {"status": "clear"}})
    assert gate.ok is True


def test_gate_unit_undeterminable():
    gate = m.evaluate_overlap_gate({"children": [], "overlap": {"status": "undeterminable"}})
    assert gate.ok is False
    assert gate.escalations
