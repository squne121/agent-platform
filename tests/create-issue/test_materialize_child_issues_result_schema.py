"""AC7: materialize emits CHILD_MATERIALIZATION_RESULT_V2; partial_failure performs NO
parent patch.
"""

from __future__ import annotations

import copy

import materialize_child_issues as m


def _ok_validate(body, kind, title):
    return m.RunResult(0, '{"status": "pass"}')


def _two_child_plan(valid_plan, valid_child):
    second = copy.deepcopy(valid_child)
    second["child_id"] = "C254-4"
    second["title"] = "実装: もう一つ"
    valid_plan["children"].append(second)
    return valid_plan


def test_result_schema_keys(valid_plan):
    def create(**kw):
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    res = m.materialize(
        valid_plan, m.Runners(validate=_ok_validate, create=create, gh=lambda a: m.RunResult(0, ""))
    )
    assert res["schema"] == "CHILD_MATERIALIZATION_RESULT_V2"
    assert set(res) == {
        "schema",
        "status",
        "created_issues",
        "affected_issues",
        "updated_parent",
        "escalation_items",
        "errors",
    }


def test_status_ok(valid_plan):
    def create(**kw):
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    res = m.materialize(
        valid_plan, m.Runners(validate=_ok_validate, create=create, gh=lambda a: m.RunResult(0, ""))
    )
    assert res["status"] == "ok"
    assert len(res["created_issues"]) == 1


def test_status_failed_when_all_fail(valid_plan):
    def create(**kw):
        return m.RunResult(1, "", "boom")

    res = m.materialize(
        valid_plan, m.Runners(validate=_ok_validate, create=create, gh=lambda a: m.RunResult(0, ""))
    )
    assert res["status"] == "failed"
    assert res["created_issues"] == []
    assert res["errors"]


def test_status_partial_failure(valid_plan, valid_child):
    plan = _two_child_plan(valid_plan, valid_child)
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')
        return m.RunResult(1, "", "second fails")

    res = m.materialize(
        plan, m.Runners(validate=_ok_validate, create=create, gh=lambda a: m.RunResult(0, ""))
    )
    assert res["status"] == "partial_failure"
    assert len(res["created_issues"]) == 1
    assert len(res["errors"]) == 1


def test_partial_failure_does_not_patch_parent(valid_plan, valid_child):
    plan = _two_child_plan(valid_plan, valid_child)
    plan["parent"]["body_sha256"] = "sha256:" + "0" * 64
    plan["parent_body_updates"] = [
        {"section": "Child Issues", "old_line": "x", "new_line": "y", "expected_match_count": 1}
    ]
    gh_calls = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "body\n")

    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')
        return m.RunResult(1, "", "second fails")

    res = m.materialize(plan, m.Runners(validate=_ok_validate, create=create, gh=gh))
    assert res["status"] == "partial_failure"
    assert res["updated_parent"] is False
    # AC7: no parent body mutation attempted on partial_failure (no gh view/edit at all).
    assert gh_calls == []


def test_mixed_escalation_blocks_parent_patch(valid_plan, valid_child):
    """Blocker 1: 1 child created + 1 child reuse_and_update_parent (escalation) must be
    partial_failure with NO parent patch — never `ok`."""
    second = copy.deepcopy(valid_child)
    second["child_id"] = "C254-9"
    second["action"] = "reuse_and_update_parent"
    valid_plan["children"].append(second)
    valid_plan["parent"]["body_sha256"] = "sha256:" + "0" * 64
    valid_plan["parent_body_updates"] = [
        {"section": "Child Issues", "old_line": "x", "new_line": "y", "expected_match_count": 1}
    ]
    gh_calls = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "body\n")

    def create(**kw):
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    res = m.materialize(valid_plan, m.Runners(validate=_ok_validate, create=create, gh=gh))
    assert res["status"] == "partial_failure"
    assert len(res["created_issues"]) == 1
    assert len(res["escalation_items"]) == 1
    assert res["updated_parent"] is False
    assert gh_calls == []  # no parent view/edit when escalations are mixed in


def test_partial_failure_records_affected_issue(valid_plan):
    """Medium 4: a create_issue_txn partial_failure that still created an issue is captured
    in affected_issues (not silently lost) and the run is not `ok`."""

    def create(**kw):
        return m.RunResult(
            1,
            '{"status":"partial_failure","issue_number":777,"issue_url":"u777"}',
            "readback failed",
        )

    res = m.materialize(
        valid_plan, m.Runners(validate=_ok_validate, create=create, gh=lambda a: m.RunResult(0, ""))
    )
    assert res["status"] == "failed"
    assert res["created_issues"] == []
    assert len(res["affected_issues"]) == 1
    assert res["affected_issues"][0]["issue_number"] == 777
    assert res["affected_issues"][0]["txn_status"] == "partial_failure"


def test_ok_attempts_parent_patch(valid_plan):
    plan = valid_plan
    plan["parent"]["body_sha256"] = None
    plan["parent_body_updates"] = []  # no-op updates, but the patch path is still entered
    gh_calls = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "body\n")

    def create(**kw):
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    res = m.materialize(plan, m.Runners(validate=_ok_validate, create=create, gh=gh))
    assert res["status"] == "ok"
    # With empty updates the patch helper short-circuits (no gh calls), but status stays ok.
    assert res["updated_parent"] is False
