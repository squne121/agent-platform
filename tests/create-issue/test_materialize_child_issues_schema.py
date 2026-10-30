"""AC1: CHILD_MATERIALIZATION_PLAN_V2 closed-schema validation (fail-closed).

Every negative case asserts a PlanValidationError is raised; the positive case asserts a
well-formed plan passes unchanged. Non-JSON input is rejected with no silent fallback.
"""

from __future__ import annotations

import json

import materialize_child_issues as m
import pytest


def test_valid_plan_passes(valid_plan):
    out = m.validate_plan(valid_plan)
    assert out is valid_plan


def test_non_dict_input_fails():
    with pytest.raises(m.PlanValidationError):
        m.validate_plan(["not", "a", "dict"])
    with pytest.raises(m.PlanValidationError):
        m.validate_plan("string")


def test_unknown_top_level_key_fails(valid_plan):
    valid_plan["surprise"] = 1
    with pytest.raises(m.PlanValidationError, match="unknown top-level key"):
        m.validate_plan(valid_plan)


def test_wrong_schema_version_fails(valid_plan):
    valid_plan["schema_version"] = 1
    with pytest.raises(m.PlanValidationError, match="schema_version"):
        m.validate_plan(valid_plan)


def test_unknown_child_key_fails(valid_plan):
    valid_plan["children"][0]["bogus"] = True
    with pytest.raises(m.PlanValidationError, match="unknown key"):
        m.validate_plan(valid_plan)


def test_duplicate_child_id_fails(valid_plan, valid_child):
    dup = dict(valid_child)
    valid_plan["children"].append(dup)
    with pytest.raises(m.PlanValidationError, match="duplicate child_id"):
        m.validate_plan(valid_plan)


def test_issue_lookup_incomplete_fails(valid_plan):
    valid_plan["issue_lookup"] = {"complete": False}
    with pytest.raises(m.PlanValidationError, match="complete is false"):
        m.validate_plan(valid_plan)


def test_invalid_action_fails(valid_plan):
    valid_plan["children"][0]["action"] = "delete_everything"
    with pytest.raises(m.PlanValidationError, match="invalid action"):
        m.validate_plan(valid_plan)


@pytest.mark.parametrize("bad_dep", ["948", 1.5, True, None])
def test_non_integer_depends_on_fails(valid_plan, bad_dep):
    valid_plan["children"][0]["depends_on"] = [bad_dep]
    with pytest.raises(m.PlanValidationError, match="depends_on"):
        m.validate_plan(valid_plan)


def test_empty_allowed_paths_fails(valid_plan):
    valid_plan["children"][0]["allowed_paths"] = []
    with pytest.raises(m.PlanValidationError, match="allowed_paths"):
        m.validate_plan(valid_plan)


def test_ac_vc_set_mismatch_fails(valid_plan):
    # AC set {AC1, AC2} but VC set {AC1, AC3}
    valid_plan["children"][0]["verification_commands"] = {"AC1": "x", "AC3": "y"}
    with pytest.raises(m.PlanValidationError, match="AC/VC mismatch"):
        m.validate_plan(valid_plan)


def test_duplicate_ac_id_fails(valid_plan):
    valid_plan["children"][0]["acceptance_criteria"] = ["AC1", "AC1"]
    valid_plan["children"][0]["verification_commands"] = {"AC1": "x"}
    with pytest.raises(m.PlanValidationError, match="duplicate AC"):
        m.validate_plan(valid_plan)


def test_invalid_label_profile_fails(valid_plan):
    valid_plan["children"][0]["label_profile"] = "admin_only"
    with pytest.raises(m.PlanValidationError, match="label_profile"):
        m.validate_plan(valid_plan)


def test_parent_body_updates_require_body_sha256(valid_plan):
    # Blocker 2: parent_body_updates without parent.body_sha256 is fail-closed.
    valid_plan["parent_body_updates"] = [
        {"section": "Child Issues", "old_line": "a", "new_line": "b", "expected_match_count": 1}
    ]
    valid_plan["parent"].pop("body_sha256", None)
    with pytest.raises(m.PlanValidationError, match="body_sha256"):
        m.validate_plan(valid_plan)


def test_parent_body_updates_reject_bad_sha_format(valid_plan):
    valid_plan["parent_body_updates"] = [
        {"section": "Child Issues", "old_line": "a", "new_line": "b", "expected_match_count": 1}
    ]
    valid_plan["parent"]["body_sha256"] = "deadbeef"  # missing sha256: prefix / wrong length
    with pytest.raises(m.PlanValidationError, match="body_sha256"):
        m.validate_plan(valid_plan)


def test_verification_command_code_fence_rejected(valid_plan):
    # Medium 1: a VC containing a code fence could break out of the rendered ```bash block.
    valid_plan["children"][0]["verification_commands"]["AC1"] = "echo ```injected"
    with pytest.raises(m.PlanValidationError, match="code fence"):
        m.validate_plan(valid_plan)


def test_allowed_path_backtick_rejected(valid_plan):
    valid_plan["children"][0]["allowed_paths"] = ["src/`evil`.ts"]
    with pytest.raises(m.PlanValidationError, match="backtick"):
        m.validate_plan(valid_plan)


def test_title_control_char_rejected(valid_plan):
    valid_plan["children"][0]["title"] = "実装: x\x00y"
    with pytest.raises(m.PlanValidationError, match="control characters"):
        m.validate_plan(valid_plan)


def test_non_json_plan_file_fails(tmp_path):
    p = tmp_path / "plan.yaml"
    p.write_text("schema_version: 2\nrepo: x\n", encoding="utf-8")  # valid YAML, not JSON
    with pytest.raises(m.PlanValidationError, match="not valid JSON"):
        m._load_plan_file(str(p))


def test_cli_returns_nonzero_on_invalid_plan(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    rc = m.main(["--plan-file", str(p)])
    assert rc == 2
