"""docs/dev/extension-surface-runtime-policy.yaml が同名 JSON Schema に対して
valid であることを検証する pytest テスト（Issue #2283 AC10 / AC13）。

VC preflight の allowlist は `python3 -c` インライン実行を許可しないため、
`uv run --locked pytest docs/dev/tests/test_extension_surface_runtime_policy_schema.py`
経由で検証する。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_YAML_PATH = REPO_ROOT / "docs" / "dev" / "extension-surface-runtime-policy.yaml"
POLICY_SCHEMA_PATH = (
    REPO_ROOT / "docs" / "dev" / "extension-surface-runtime-policy.schema.json"
)


def _load_policy() -> dict:
    with POLICY_YAML_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_schema() -> dict:
    with POLICY_SCHEMA_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_policy_yaml_exists() -> None:
    assert POLICY_YAML_PATH.is_file()


def test_schema_json_exists() -> None:
    assert POLICY_SCHEMA_PATH.is_file()


def test_schema_json_is_draft_2020_12() -> None:
    schema = _load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_policy_yaml_validates_against_schema() -> None:
    policy = _load_policy()
    schema = _load_schema()
    # jsonschema.validate() raises on failure; a clean return means valid.
    jsonschema.validate(instance=policy, schema=schema)


def test_all_rule_verification_profile_references_exist() -> None:
    """AC13: 各 rule の verification_profile 参照先が top-level
    verification_profiles に実在することを、schema validation とは別に
    直接クロス参照して確認する。
    """
    policy = _load_policy()
    profile_ids = set(policy["verification_profiles"].keys())
    for rule in policy["rules"]:
        assert rule["verification_profile"] in profile_ids, (
            f"rule {rule['id']!r} references unknown verification_profile "
            f"{rule['verification_profile']!r}"
        )


def test_all_rules_have_at_least_one_project_and_one_runtime_resolved_selector() -> None:
    """AC12: 各 rule の selectors[] に project（repository 相対）と
    runtime-resolved（user/managed/plugin/session/cli のいずれか）の
    両方が区別して存在することを確認する。
    """
    policy = _load_policy()
    for rule in policy["rules"]:
        scopes = {selector["source_scope"] for selector in rule["selectors"]}
        assert "project" in scopes, f"rule {rule['id']!r} missing project selector"
        runtime_resolved_scopes = scopes - {"project"}
        assert runtime_resolved_scopes, (
            f"rule {rule['id']!r} missing a runtime-resolved selector"
        )


def test_auto_mode_assumption_min_version_is_2_1_83_or_higher() -> None:
    """AC15: Auto mode 関連 claim の min_claude_code_version が 2.1.83 以上。"""
    policy = _load_policy()
    auto_mode_claims = [
        a for a in policy["assumptions"] if a.get("applicability") == "auto_mode"
    ]
    assert auto_mode_claims, "no auto_mode assumption claim found"
    for claim in auto_mode_claims:
        version = claim["min_claude_code_version"]
        assert tuple(int(p) for p in version.split(".")) >= (2, 1, 83), (
            f"claim {claim['claim_id']!r} min_claude_code_version {version!r} "
            "is below 2.1.83"
        )


# ---------------------------------------------------------------------------
# Issue #2356 AC1 / AC6: `issue_time_enforcement` field on the project
# selector object definition accepts `hard` / `advisory` and rejects
# invalid values (negative case).
# ---------------------------------------------------------------------------


def _find_skill_invocation_rule(policy: dict) -> dict:
    for rule in policy["rules"]:
        if rule["id"] == "skill-invocation-procedure-or-contract-change":
            return rule
    raise AssertionError(
        "policy yaml has no 'skill-invocation-procedure-or-contract-change' rule"
    )


def test_issue_time_enforcement_field_present_and_valid_on_split_selectors() -> None:
    """AC1/AC6 positive case: the live policy YAML's
    `skill-invocation-procedure-or-contract-change` rule already declares
    `issue_time_enforcement: hard` / `advisory` on its project selectors,
    and the whole document validates against the schema (which must
    therefore accept the field with these enum values).
    """
    policy = _load_policy()
    schema = _load_schema()
    rule = _find_skill_invocation_rule(policy)
    project_selectors = [s for s in rule["selectors"] if s["source_scope"] == "project"]
    declared_values = {s.get("issue_time_enforcement") for s in project_selectors}
    assert declared_values == {"hard", "advisory"}
    # A clean return (no exception) means the schema accepts these values.
    jsonschema.validate(instance=policy, schema=schema)


def test_issue_time_enforcement_field_rejects_invalid_value() -> None:
    """AC1/AC6 negative case: a selector declaring an `issue_time_enforcement`
    value outside the `hard`/`advisory` enum must fail schema validation.
    """
    policy = copy.deepcopy(_load_policy())
    schema = _load_schema()
    rule = _find_skill_invocation_rule(policy)
    project_selector = next(s for s in rule["selectors"] if s["source_scope"] == "project")
    project_selector["issue_time_enforcement"] = "not-a-real-enforcement-level"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=policy, schema=schema)


# ---------------------------------------------------------------------------
# Issue #2356 AC2: the `skill-invocation-procedure-or-contract-change` rule,
# after the selector split, has exactly a `.claude/skills/**/SKILL.md`
# selector with `issue_time_enforcement: hard` and a
# `.claude/skills/**/scripts/**` selector with `issue_time_enforcement:
# advisory` -- asserted on the parsed YAML structure, not a loose text match.
# ---------------------------------------------------------------------------


def test_skill_invocation_rule_selectors_split_skill_md_hard_scripts_advisory() -> None:
    policy = _load_policy()
    rule = _find_skill_invocation_rule(policy)
    project_selectors = [s for s in rule["selectors"] if s["source_scope"] == "project"]

    skill_md_selectors = [
        s for s in project_selectors if s.get("path_globs") == [".claude/skills/**/SKILL.md"]
    ]
    scripts_selectors = [
        s for s in project_selectors if s.get("path_globs") == [".claude/skills/**/scripts/**"]
    ]

    assert len(skill_md_selectors) == 1, (
        "expected exactly one project selector with path_globs == "
        "['.claude/skills/**/SKILL.md']"
    )
    assert len(scripts_selectors) == 1, (
        "expected exactly one project selector with path_globs == "
        "['.claude/skills/**/scripts/**']"
    )
    assert skill_md_selectors[0]["issue_time_enforcement"] == "hard"
    assert scripts_selectors[0]["issue_time_enforcement"] == "advisory"

    # No single project selector should still carry both globs bundled
    # together (the pre-#2356 shape this Issue splits apart).
    for selector in project_selectors:
        globs = selector.get("path_globs") or []
        assert not (
            ".claude/skills/**/SKILL.md" in globs and ".claude/skills/**/scripts/**" in globs
        ), "SKILL.md and scripts/** must live in separate project selectors"


# ---------------------------------------------------------------------------
# Issue #2961: rule-level optional `issue_time_exemption` (comment-only
# exemption opt-in). Only `claude-gpt-lifecycle-invocation-change` may carry
# it; the closed validator pins its key names / enums.
# ---------------------------------------------------------------------------

_CLAUDE_GPT_RULE_ID = "claude-gpt-lifecycle-invocation-change"


def _rule_by_id(policy: dict, rule_id: str) -> dict:
    for rule in policy["rules"]:
        if rule["id"] == rule_id:
            return rule
    raise AssertionError(f"policy yaml has no {rule_id!r} rule")


def test_issue_time_exemption_only_on_claude_gpt_rule() -> None:
    """AC7: the live policy has `issue_time_exemption` on the claude-gpt rule
    only, with the closed key names / enum values, and the schema rejects it
    on every other rule (hook / skill / subagent / agent)."""
    policy = _load_policy()
    schema = _load_schema()

    carriers = [rule["id"] for rule in policy["rules"] if "issue_time_exemption" in rule]
    assert carriers == [_CLAUDE_GPT_RULE_ID]
    assert _rule_by_id(policy, _CLAUDE_GPT_RULE_ID)["issue_time_exemption"] == {
        "declaration_key": "executable_semantics_unchanged",
        "mode": "exact_file_path_git_diff_vc",
    }
    jsonschema.validate(instance=policy, schema=schema)

    claude_gpt_exemption = _rule_by_id(policy, _CLAUDE_GPT_RULE_ID)["issue_time_exemption"]
    for rule in policy["rules"]:
        if rule["id"] == _CLAUDE_GPT_RULE_ID:
            continue
        mutated = copy.deepcopy(policy)
        _rule_by_id(mutated, rule["id"])["issue_time_exemption"] = copy.deepcopy(
            claude_gpt_exemption
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=mutated, schema=schema)


@pytest.mark.parametrize(
    "bad_value",
    [
        {"declaration_key": "something_else", "mode": "exact_file_path_git_diff_vc"},
        {"declaration_key": "executable_semantics_unchanged", "mode": "any_path"},
        {"declaration_key": "executable_semantics_unchanged"},
        {"mode": "exact_file_path_git_diff_vc"},
        {
            "declaration_key": "executable_semantics_unchanged",
            "mode": "exact_file_path_git_diff_vc",
            "unexpected": True,
        },
        "enabled",
    ],
)
def test_issue_time_exemption_closed_validator_rejects_invalid_shapes(bad_value) -> None:
    policy = copy.deepcopy(_load_policy())
    _rule_by_id(policy, _CLAUDE_GPT_RULE_ID)["issue_time_exemption"] = bad_value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=policy, schema=_load_schema())


def test_existing_exceptions_are_not_altered_by_the_exemption_property() -> None:
    """The descriptive `exceptions[]` (human_evidence_required / owner) stay
    exactly as before; the exemption is a separate rule-level property."""
    rule = _rule_by_id(_load_policy(), _CLAUDE_GPT_RULE_ID)
    assert [e["id"] for e in rule["exceptions"]] == [
        "claude-gpt-proven-not-distributed",
        "claude-gpt-production-consumer-inventory-empty",
    ]
    assert all(
        e["evaluation_mode"] == "human_evidence_required" and e["approval_authority"] == "owner"
        for e in rule["exceptions"]
    )
