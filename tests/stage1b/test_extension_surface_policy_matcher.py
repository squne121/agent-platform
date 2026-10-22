"""Tests for `extension_surface_policy_matcher.py` (Issue #2290).

AC1-AC5 exercise the shared evaluator directly. AC7 is the cross-skill
parity test: given the same fixture body, `.claude/skills/review-issue`'s
`check_issue_contract.py` and `.claude/skills/issue-contract-review`'s
`contract_readiness_check.py` must return the same verdict because both
call the exact same `evaluate_issue_risk_trigger()` function in this
module (structural parity, not independently re-derived logic).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import extension_surface_policy_matcher  # noqa: E402
from extension_surface_policy_matcher import (  # noqa: E402
    DECISION_RANK,
    evaluate_allowed_paths,
    evaluate_issue_risk_trigger,
    load_policy,
)

_REPO_ROOT = _GUARDS_DIR.parents[1]
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CONTRACT_READINESS_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"

# PR #2370 OWNER review fix_delta (P1-1): `unknown_surface_policy` is now a
# required, strictly-validated mapping on every policy passed to
# `evaluate_allowed_paths`/`evaluate_issue_risk_trigger` (not just the real
# production YAML) -- a synthetic fixture policy that previously omitted the
# key entirely would now fail closed with `PolicyLoadError` instead of
# quietly having "no candidate perimeter". Every synthetic fixture policy
# dict below that is exercised through the shared evaluator must therefore
# declare a minimal, structurally valid `unknown_surface_policy` -- fixtures
# are fixed to carry a valid one rather than the validation being loosened
# back to optional (Issue #2339, PR #2370 P1-1 fix_delta explicit
# instruction). The glob deliberately does not overlap with any Allowed
# Path entry used by the tests below, so it never changes any existing
# assertion about `matched_rules` / `final_decision` / `verification_profiles`.
_MINIMAL_VALID_UNKNOWN_SURFACE_POLICY = {
    "decision": "human_judgment",
    "gate": "advisory",
    "project_candidate_path_globs": ["zzz-unused-synthetic-candidate-perimeter/**"],
}


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# AC1: exact risky Allowed Path + not_applicable -> needs_fix
# ---------------------------------------------------------------------------


def test_exact_path_risky_not_applicable():
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/agents/implementation-worker.md"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "needs_fix"
    assert result["policy_evaluation"]["has_match"] is True
    assert any(
        m["match_kind"] == "exact" for m in result["policy_evaluation"]["matched_rules"][0]["matches"]
    )
    assert result["reasons"]


# ---------------------------------------------------------------------------
# AC2: wildcard Allowed Path with conservative (literal/static prefix)
# overlap -> needs_fix. This is a syntactic candidate-discovery match, not a
# semantic diff judgment -- the comment below documents that distinction.
# ---------------------------------------------------------------------------


def test_wildcard_conservative_overlap():
    # ".claude/skills/some-new-skill/**" shares the static prefix
    # [".claude", "skills"] with the policy rule selector
    # ".claude/skills/**/SKILL.md" -- this is a *conservative* candidate
    # match (literal/static prefix comparison), not proof that the two
    # path spaces actually intersect once the wildcard segments expand.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/skills/some-new-skill/**"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "needs_fix"
    matched_rule = result["policy_evaluation"]["matched_rules"][0]
    assert matched_rule["matches"][0]["match_kind"] == "conservative_wildcard_prefix"


# ---------------------------------------------------------------------------
# AC3: docs-only Allowed Paths + not_applicable -> approve (no candidate match)
# ---------------------------------------------------------------------------


def test_docs_only_not_applicable_approve():
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=["docs/dev/extension-surface-runtime-policy.yaml", "docs/product/requirements.md"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["policy_evaluation"]["has_match"] is False
    assert result["reasons"] == []


# ---------------------------------------------------------------------------
# AC4: multiple matched rules -> single most_restrictive final_decision
# ---------------------------------------------------------------------------


_MULTI_RULE_POLICY = {
    "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
    "unknown_surface_policy": _MINIMAL_VALID_UNKNOWN_SURFACE_POLICY,
    "rules": [
        {
            "id": "rule-deferred-scope",
            "selectors": [
                {"source_scope": "project", "path_globs": ["fixtures/deferred-surface/**"]},
            ],
            "default_decision": "deferred",
            "verification_profile": "profile-a",
        },
        {
            "id": "rule-immediate-scope",
            "selectors": [
                {"source_scope": "project", "path_globs": ["fixtures/**"]},
            ],
            "default_decision": "immediate",
            "verification_profile": "profile-b",
        },
    ],
}


def test_multiple_rule_most_restrictive():
    result = evaluate_allowed_paths(
        allowed_path_entries=["fixtures/deferred-surface/foo.py"],
        policy=_MULTI_RULE_POLICY,
    )
    assert len(result["matched_rules"]) == 2
    assert result["final_decision"] == "immediate"
    assert DECISION_RANK["immediate"] > DECISION_RANK["deferred"]


# ---------------------------------------------------------------------------
# AC5: verification_profiles union derived from matched rules, no Issue-body
# duplication required.
# ---------------------------------------------------------------------------


def test_verification_profile_union_derived():
    result = evaluate_allowed_paths(
        allowed_path_entries=["fixtures/deferred-surface/foo.py"],
        policy=_MULTI_RULE_POLICY,
    )
    assert result["verification_profiles"] == ["profile-a", "profile-b"]


# ---------------------------------------------------------------------------
# AC7: review-issue / issue-contract-review parity via the shared evaluator
# ---------------------------------------------------------------------------

_PARITY_FIXTURE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for parity fixture testing purposes only.

## Acceptance Criteria

- [ ] AC1: concrete AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- .claude/agents/implementation-worker.md

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: parity fixture, no runtime execution needed

## Required Skills

none
"""


def test_review_issue_and_contract_readiness_parity():
    check_issue_contract = _load_module_from_path(
        "check_issue_contract_for_ext_surface_parity_test",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_ext_surface_parity_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _PARITY_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(_PARITY_FIXTURE_BODY)

    review_issue_needs_fix = review_issue_status == check_issue_contract.CheckResult.FAIL
    readiness_needs_fix = bool(readiness_errors)

    assert review_issue_needs_fix == readiness_needs_fix
    assert review_issue_needs_fix is True
    assert bool(review_issue_reasons) == bool(readiness_errors)


def test_load_policy_reads_real_policy_yaml():
    # Sanity check that the real repository policy file loads and matches
    # against a known risky selector -- guards against the fixture-only
    # tests above silently diverging from the production YAML shape.
    policy = load_policy()
    assert policy.get("schema_version") == "v2"
    result = evaluate_allowed_paths([".claude/hooks/some_hook.py"], policy=policy)
    assert result["has_match"] is True
    assert result["final_decision"] == "immediate"


# ---------------------------------------------------------------------------
# P0 (Issue #2290, PR #2335 OWNER review fix_delta -- highest priority):
# Issue #2290's own declared Allowed Paths must not self-violate the gate it
# introduces. `.claude/skills/**/scripts/**` matches are candidate-discovery
# only ("advisory") and must not force needs_fix, unlike
# `.claude/skills/**/SKILL.md` matches which remain a hard block.
# ---------------------------------------------------------------------------


def test_self_application_skill_scripts_only_is_advisory_not_hard_block():
    # Issue #2290's own Allowed Paths (as declared in the live Issue body):
    # only touches skill *script* files, not any SKILL.md, and declares
    # decision: not_applicable. This must resolve to `approve`, not
    # `needs_fix` -- otherwise the gate this Issue introduces would
    # self-violate on its own contract.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[
            ".claude/skills/review-issue/scripts/check_issue_contract.py",
            ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
        ],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["reasons"] == []
    policy_evaluation = result["policy_evaluation"]
    assert policy_evaluation["has_match"] is True
    assert policy_evaluation["final_decision"] is None
    matched_rule = policy_evaluation["matched_rules"][0]
    assert matched_rule["enforcement"] == "advisory"
    assert all(
        match["path_glob"] == ".claude/skills/**/scripts/**" for match in matched_rule["matches"]
    )


def test_skill_md_match_remains_hard_block():
    # Contrast case: a SKILL.md match (skill runtime semantics itself) must
    # keep forcing needs_fix as before -- only the scripts/** selector was
    # downgraded to advisory.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/skills/some-skill/SKILL.md"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "needs_fix"
    matched_rule = result["policy_evaluation"]["matched_rules"][0]
    assert matched_rule["enforcement"] == "hard"


def test_wildcard_entry_ambiguous_prefix_resolves_to_advisory_not_hard():
    # Root cause of the P0 re-fix (PR #2335, second fix_delta): a *wildcard*
    # Allowed Path entry whose conservative static prefix matches BOTH
    # ``.claude/skills/**/scripts/**`` (candidate-only/advisory) and
    # ``.claude/skills/**/SKILL.md`` (hard) within the same rule must not
    # be forced hard purely because ``SKILL.md`` happened to be iterated
    # first in the rule's ``path_globs`` list. This is exactly the shape
    # of Issue #2290's own wildcard Allowed Path entries (e.g.
    # ``.claude/skills/review-issue/tests/**``).
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/skills/review-issue/tests/**"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["reasons"] == []
    policy_evaluation = result["policy_evaluation"]
    assert policy_evaluation["has_match"] is True
    assert policy_evaluation["final_decision"] is None
    matched_rule = policy_evaluation["matched_rules"][0]
    assert matched_rule["enforcement"] == "advisory"
    assert all(
        match["path_glob"] == ".claude/skills/**/scripts/**" for match in matched_rule["matches"]
    )


def test_wildcard_entry_matching_only_skill_md_glob_still_hard():
    # Contrast case for the ambiguity fix: a wildcard entry whose static
    # prefix only reaches a rule glob that is NOT candidate-only (no
    # ambiguity at all, since the rule in this fixture policy only has one
    # glob) must remain a hard match, same as before the fix.
    single_glob_policy = {
        "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
        "unknown_surface_policy": _MINIMAL_VALID_UNKNOWN_SURFACE_POLICY,
        "rules": [
            {
                "id": "rule-skill-md-only",
                "selectors": [
                    {"source_scope": "project", "path_globs": [".claude/skills/**/SKILL.md"]},
                ],
                "default_decision": "immediate",
                "verification_profile": "profile-skill-md",
            },
        ],
    }
    result = evaluate_allowed_paths(
        allowed_path_entries=[".claude/skills/some-skill/**"],
        policy=single_glob_policy,
    )
    matched_rule = result["matched_rules"][0]
    assert matched_rule["enforcement"] == "hard"
    assert result["final_decision"] == "immediate"


# ---------------------------------------------------------------------------
# P0 re-fix (Issue #2290, PR #2335 second OWNER review fix_delta): the full
# live Issue #2290 Allowed Paths set -- exact script paths AND all wildcard
# entries -- must not self-violate the gate this Issue introduces, when run
# through BOTH consumer functions (review-issue and issue-contract-review).
# The previous P0 fix's self-application test only exercised the two exact
# script paths and missed the wildcard entries that actually trigger the
# glob-iteration-order bug.
# ---------------------------------------------------------------------------

_ISSUE_2290_LIVE_ALLOWED_PATHS = [
    "scripts/agent-guards/extension_surface_policy_matcher.py",
    "scripts/agent-guards/tests/test_extension_surface_policy_matcher.py",
    ".claude/skills/review-issue/scripts/check_issue_contract.py",
    ".claude/skills/review-issue/fixtures/**",
    ".claude/skills/review-issue/schemas/**",
    ".claude/skills/review-issue/tests/**",
    ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
    ".claude/skills/issue-contract-review/tests/**",
    ".claude/skills/issue-contract-review/scripts/tests/test_baseline_vc_preflight_timeout_classification.py",
]

_ISSUE_2290_LIVE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for Issue #2290 self-application fixture.

## Acceptance Criteria

- [ ] AC1: concrete AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- scripts/agent-guards/extension_surface_policy_matcher.py
- scripts/agent-guards/tests/test_extension_surface_policy_matcher.py
- .claude/skills/review-issue/scripts/check_issue_contract.py
- .claude/skills/review-issue/fixtures/**
- .claude/skills/review-issue/schemas/**
- .claude/skills/review-issue/tests/**
- .claude/skills/issue-contract-review/scripts/contract_readiness_check.py
- .claude/skills/issue-contract-review/tests/**
- .claude/skills/issue-contract-review/scripts/tests/test_baseline_vc_preflight_timeout_classification.py

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: self-application fixture, no runtime execution needed

## Required Skills

none
"""


def test_shared_evaluator_self_application_full_allowed_paths_set_is_approve():
    # Direct shared-evaluator exercise of the FULL live Issue #2290 Allowed
    # Paths set (exact + all 4 wildcard entries), not just the 2 exact
    # script paths the earlier P0 fix's test covered.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=_ISSUE_2290_LIVE_ALLOWED_PATHS,
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["reasons"] == []
    matched_rule = result["policy_evaluation"]["matched_rules"][0]
    assert matched_rule["enforcement"] == "advisory"


def test_mixed_scripts_and_skill_md_match_is_hard():
    # A rule with at least one hard hit (SKILL.md) alongside an advisory hit
    # (scripts/**) must still be classified "hard" overall for that rule.
    result = evaluate_allowed_paths(
        allowed_path_entries=[
            ".claude/skills/foo/scripts/bar.py",
            ".claude/skills/foo/SKILL.md",
        ],
    )
    matched_rule = next(
        r for r in result["matched_rules"] if r["rule_id"] == "skill-invocation-procedure-or-contract-change"
    )
    assert matched_rule["enforcement"] == "hard"
    assert result["final_decision"] == "immediate"


# ---------------------------------------------------------------------------
# P1-2 (Issue #2290, PR #2335 OWNER review fix_delta): load_policy() must
# fail closed (raise PolicyLoadError) on structurally incompatible policy
# YAML instead of silently degrading to a normal "no candidate match".
# ---------------------------------------------------------------------------


def test_load_policy_rejects_unsupported_schema_version(tmp_path):
    from extension_surface_policy_matcher import PolicyLoadError

    bad_policy_path = tmp_path / "bad_schema_version.yaml"
    bad_policy_path.write_text(
        "schema_version: v1\n"
        "resolution:\n"
        "  multiple_matches: evaluate_all\n"
        "  final_decision: most_restrictive\n"
        "rules:\n"
        "  - id: r1\n"
        "    selectors:\n"
        "      - source_scope: project\n"
        "        path_globs: ['foo/**']\n"
        "    default_decision: immediate\n"
    )
    try:
        load_policy(bad_policy_path)
        assert False, "expected PolicyLoadError"
    except PolicyLoadError:
        pass


def test_load_policy_rejects_empty_rules(tmp_path):
    from extension_surface_policy_matcher import PolicyLoadError

    bad_policy_path = tmp_path / "empty_rules.yaml"
    bad_policy_path.write_text(
        "schema_version: v2\n"
        "resolution:\n"
        "  multiple_matches: evaluate_all\n"
        "  final_decision: most_restrictive\n"
        "rules: []\n"
    )
    try:
        load_policy(bad_policy_path)
        assert False, "expected PolicyLoadError"
    except PolicyLoadError:
        pass


def test_load_policy_rejects_unknown_resolution_value(tmp_path):
    from extension_surface_policy_matcher import PolicyLoadError

    bad_policy_path = tmp_path / "unknown_resolution.yaml"
    bad_policy_path.write_text(
        "schema_version: v2\n"
        "resolution:\n"
        "  multiple_matches: first_match_only\n"
        "  final_decision: most_restrictive\n"
        "rules:\n"
        "  - id: r1\n"
        "    selectors:\n"
        "      - source_scope: project\n"
        "        path_globs: ['foo/**']\n"
        "    default_decision: immediate\n"
    )
    try:
        load_policy(bad_policy_path)
        assert False, "expected PolicyLoadError"
    except PolicyLoadError:
        pass


def test_load_policy_accepts_minimal_valid_policy(tmp_path):
    good_policy_path = tmp_path / "good.yaml"
    good_policy_path.write_text(
        "schema_version: v2\n"
        "resolution:\n"
        "  multiple_matches: evaluate_all\n"
        "  final_decision: most_restrictive\n"
        "rules:\n"
        "  - id: r1\n"
        "    selectors:\n"
        "      - source_scope: project\n"
        "        path_globs: ['foo/**']\n"
        "    default_decision: immediate\n"
    )
    policy = load_policy(good_policy_path)
    assert policy["schema_version"] == "v2"


# ---------------------------------------------------------------------------
# Issue #2356 AC3: `CANDIDATE_ONLY_PATH_GLOBS` hardcoded frozenset is removed
# in favour of the YAML/schema-driven `issue_time_enforcement` field.
# ---------------------------------------------------------------------------


def test_candidate_only_path_globs_removed():
    assert not hasattr(extension_surface_policy_matcher, "CANDIDATE_ONLY_PATH_GLOBS")


# ---------------------------------------------------------------------------
# Issue #2356 AC5: a project selector that intentionally OMITS
# `issue_time_enforcement` must be treated as `hard` -- this is the required
# runtime fallback for existing/future rules that never declare the field
# (e.g. `claude-gpt-lifecycle-invocation-change`). This is a *synthetic*
# selector constructed directly in this test, independent of any existing
# rule's current unlabeled state, so the backward-compat meaning stays fixed
# even if an existing rule later gains an explicit `issue_time_enforcement`.
# ---------------------------------------------------------------------------


def test_selector_omitting_issue_time_enforcement_defaults_to_hard():
    synthetic_policy = {
        "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
        "unknown_surface_policy": _MINIMAL_VALID_UNKNOWN_SURFACE_POLICY,
        "rules": [
            {
                "id": "synthetic-rule-no-issue-time-enforcement",
                "selectors": [
                    # Deliberately omits `issue_time_enforcement` entirely.
                    {"source_scope": "project", "path_globs": ["synthetic/no-enforcement-field/**"]},
                ],
                "default_decision": "immediate",
                "verification_profile": "profile-synthetic",
            },
        ],
    }
    result = evaluate_allowed_paths(
        allowed_path_entries=["synthetic/no-enforcement-field/foo.py"],
        policy=synthetic_policy,
    )
    matched_rule = result["matched_rules"][0]
    assert matched_rule["enforcement"] == "hard"
    assert result["final_decision"] == "immediate"
    assert matched_rule["matches"][0]["issue_time_enforcement"] == "hard"


# ---------------------------------------------------------------------------
# PR #2359 OWNER review fix_delta (iteration 1, P1 blocker): an invalid
# `issue_time_enforcement` value on a project selector (e.g. a typo such as
# "hrad" instead of "hard") must fail closed -- raising `PolicyLoadError` --
# rather than silently flowing through as a string that is neither "hard"
# nor "advisory", failing the `== "hard"` comparison, and thereby degrading
# the rule to "advisory" (a malformed policy value must never silently
# WEAKEN the gate; this is the same class of self-application false-negative
# that caused the P0 regression in PR #2335 / Issue #2290).
# ---------------------------------------------------------------------------


def test_invalid_issue_time_enforcement_fails_closed():
    bad_policy = {
        "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
        "unknown_surface_policy": _MINIMAL_VALID_UNKNOWN_SURFACE_POLICY,
        "rules": [
            {
                "id": "synthetic-rule-invalid-issue-time-enforcement",
                "selectors": [
                    {
                        "source_scope": "project",
                        "path_globs": ["synthetic/invalid-enforcement-field/**"],
                        "issue_time_enforcement": "hrad",
                    },
                ],
                "default_decision": "immediate",
                "verification_profile": "profile-synthetic-invalid",
            },
        ],
    }
    try:
        evaluate_allowed_paths(
            allowed_path_entries=["synthetic/invalid-enforcement-field/foo.py"],
            policy=bad_policy,
        )
        assert False, "expected PolicyLoadError for invalid issue_time_enforcement value"
    except extension_surface_policy_matcher.PolicyLoadError as exc:
        assert "hrad" in str(exc)


# ---------------------------------------------------------------------------
# Issue #2771 PR #2780 OWNER F1 review: `_extract_rva_yaml_field()` /
# `parse_runtime_assertion_bindings()` / `extract_applicable_acs()` must not
# regress a structurally normal Issue body into a false "field absent"
# reading just because of a trailing comment, `yaml.safe_dump()`-style
# same-indent block sequence, or a bullet-prefixed field key.
# ---------------------------------------------------------------------------

_HOOK_BINDING_YAML_INDENTED = """\
decision: immediate
applicable_acs: [AC1]
runtime_assertion_bindings:
  - profile: hook-chain-runtime-smoke
    assertion: all_matching_hooks_observed
    ac: AC1
  - profile: hook-chain-runtime-smoke
    assertion: sibling_side_effect_inventory_complete
    ac: AC1
"""

_EXPECTED_HOOK_BINDINGS = [
    {"profile": "hook-chain-runtime-smoke", "assertion": "all_matching_hooks_observed", "ac": "1"},
    {
        "profile": "hook-chain-runtime-smoke",
        "assertion": "sibling_side_effect_inventory_complete",
        "ac": "1",
    },
]


def test_binding_key_line_trailing_comment_does_not_truncate_block():
    """A trailing YAML comment on `runtime_assertion_bindings:`'s own key
    line must not be mistaken for a non-empty inline value that swallows
    the nested list (Issue #2771 PR #2780 OWNER F1 review)."""
    commented = _HOOK_BINDING_YAML_INDENTED.replace(
        "runtime_assertion_bindings:",
        "runtime_assertion_bindings: # このACで両方を検証する",
    )
    bindings, malformed = extension_surface_policy_matcher.parse_runtime_assertion_bindings(commented)
    assert malformed == []
    assert bindings == _EXPECTED_HOOK_BINDINGS

    baseline_bindings, baseline_malformed = extension_surface_policy_matcher.parse_runtime_assertion_bindings(
        _HOOK_BINDING_YAML_INDENTED
    )
    assert baseline_malformed == []
    assert bindings == baseline_bindings


def test_binding_yaml_safe_dump_same_indent_sequence_is_parsed():
    """`yaml.safe_dump()`'s own output style -- a mapping value's sequence
    items at the SAME indentation as the key, not indented deeper -- is
    valid YAML and must parse to the same bindings as the indented form
    (Issue #2771 PR #2780 OWNER F1 review)."""
    dumped_style = (
        "decision: immediate\n"
        "applicable_acs: [AC1]\n"
        "runtime_assertion_bindings:\n"
        "- profile: hook-chain-runtime-smoke\n"
        "  assertion: all_matching_hooks_observed\n"
        "  ac: AC1\n"
        "- profile: hook-chain-runtime-smoke\n"
        "  assertion: sibling_side_effect_inventory_complete\n"
        "  ac: AC1\n"
    )
    bindings, malformed = extension_surface_policy_matcher.parse_runtime_assertion_bindings(dumped_style)
    assert malformed == []
    assert bindings == _EXPECTED_HOOK_BINDINGS


def test_bullet_prefixed_applicable_acs_field_is_read():
    """An `applicable_acs` field written as its own bullet item
    (`- applicable_acs: [AC1]`), matching the existing `- decision:` /
    `- reason:` bullet authoring convention, must not be read as absent
    (Issue #2771 PR #2780 OWNER F1 review)."""
    bulleted = "- decision: immediate\n- applicable_acs: [AC1]\n"
    assert extension_surface_policy_matcher.extract_applicable_acs(bulleted) == {"1"}


def test_bullet_prefixed_field_same_indent_sibling_bullet_not_swallowed():
    """A bullet-prefixed `runtime_assertion_bindings:` key's same-indent
    continuation rule must NOT reach across into the next, unrelated
    sibling bullet field (Issue #2771 PR #2780 OWNER F1 review: the
    same-indent continuation only applies to the plain, non-bullet key
    form)."""
    bulleted_with_sibling = (
        "- decision: immediate\n"
        "- runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        "    assertion: all_matching_hooks_observed\n"
        "    ac: AC1\n"
        "- reason: unrelated sibling field text\n"
    )
    bindings, malformed = extension_surface_policy_matcher.parse_runtime_assertion_bindings(
        bulleted_with_sibling
    )
    assert malformed == []
    assert bindings == [
        {"profile": "hook-chain-runtime-smoke", "assertion": "all_matching_hooks_observed", "ac": "1"}
    ]


# ---------------------------------------------------------------------------
# Issue #2771 PR #2780 OWNER F3 review: `extract_ac_numbers()` must only
# treat a genuine task-list AC declaration as "real" -- not every
# `AC<N>`-shaped substring anywhere in the Acceptance Criteria section text
# (a known false-positive shape independently tracked for review-issue's C5
# checker in Issue #1712; this fix is local to this function only).
# ---------------------------------------------------------------------------


def test_ac_number_in_prose_explaining_a_prior_proposal_is_not_declared():
    """GIVEN a single genuine AC1 task-list item whose own description text
    mentions a previous proposal's number (AC99)
    WHEN AC numbers are extracted
    THEN only AC1 is reported as declared -- AC99 is prose, not a real AC."""
    section = "- [ ] AC1: 現在の受入条件。旧案のAC99は参考情報であり、今回の受入条件ではない。\n"
    assert extension_surface_policy_matcher.extract_ac_numbers(section) == {"1"}


def test_ac_number_only_inside_fenced_example_is_not_declared():
    """GIVEN a real AC1 task-list item plus a fenced code example that shows
    what an `AC99` line might look like
    WHEN AC numbers are extracted
    THEN AC99 (fenced-only) is not counted as a real, declared AC."""
    section = (
        "- [ ] AC1: concrete AC\n"
        "\n"
        "```markdown\n"
        "- [ ] AC99: example only, not a real AC\n"
        "```\n"
    )
    assert extension_surface_policy_matcher.extract_ac_numbers(section) == {"1"}


def test_ac_number_declared_as_checked_task_list_item_is_read():
    """A checked task-list item (`- [x] AC2: ...`) is still a genuine
    declared AC, not just the unchecked `- [ ]` form."""
    section = "- [x] AC2: already-completed AC\n"
    assert extension_surface_policy_matcher.extract_ac_numbers(section) == {"2"}


# ---------------------------------------------------------------------------
# Issue #2961: issue-time comment-only exemption (claude-gpt rule only)
# ---------------------------------------------------------------------------

import dataclasses  # noqa: E402

import pytest  # noqa: E402

from extension_surface_policy_matcher import (  # noqa: E402
    build_ac_vc_commands,
    evaluate_runtime_assertion_binding_coverage,
    format_issue_time_exemption_lines,
)

_CLAUDE_GPT_RULE_ID = "claude-gpt-lifecycle-invocation-change"
_CLAUDE_GPT_PROFILE = "claude-gpt-process-io-smoke"
_LIB_SH = "scripts/claude-gpt/lib.sh"
_GIT_DIFF_LIB_SH = f"git diff origin/main -- {_LIB_SH}"


@dataclasses.dataclass
class _FakeVcEntry:
    """Duck-typed stand-in for ``VcCommandEntry`` (ac_refs / command only)."""

    ac_refs: set
    command: str


_AC_SECTION = (
    "- [ ] AC1: lib.sh の comment だけが変わる\n"
    "- [ ] AC2: 別の検証\n"
)


def _rva(declaration: str | None, decision: str = "not_applicable") -> str:
    lines = [f"- decision: {decision}", "- reason: comment-only"]
    if declaration is not None:
        lines.append(f"- executable_semantics_unchanged: {declaration}")
    return "\n".join(lines)


_DECL_AC1 = f"{{rule: {_CLAUDE_GPT_RULE_ID}, ac: AC1}}"


def _vc(*pairs: tuple[str, str]) -> dict[str, tuple[str, ...]]:
    """``(ac_label, command)`` pairs -> ``ac_vc_commands`` via the shared helper."""
    return build_ac_vc_commands(_FakeVcEntry({ac}, cmd) for ac, cmd in pairs)


def _evaluate_both(
    paths: list[str],
    rva: str,
    ac_section: str = _AC_SECTION,
    ac_vc_commands=None,
    declared_decision: str = "not_applicable",
):
    risk = evaluate_issue_risk_trigger(
        allowed_path_entries=paths,
        declared_decision=declared_decision,
        rva_section_text=rva,
        ac_section_text=ac_section,
        ac_vc_commands=ac_vc_commands,
    )
    coverage = evaluate_runtime_assertion_binding_coverage(
        allowed_path_entries=paths,
        rva_section_text=rva,
        ac_section_text=ac_section,
        ac_vc_refs=set((ac_vc_commands or {}).keys()),
        ac_vc_commands=ac_vc_commands,
    )
    return risk, coverage


def test_claude_gpt_comment_only_declaration_not_hard_match():
    """AC1: the #2956-shaped fixture yields neither EXTSURF001 (risk-trigger
    needs_fix) nor RUNTIMEASSERT001 (binding coverage needs_fix) and carries
    the applied exemption in `issue_time_exemptions`."""
    risk, coverage = _evaluate_both(
        [_LIB_SH], _rva(_DECL_AC1), ac_vc_commands=_vc(("AC1", _GIT_DIFF_LIB_SH))
    )
    expected = [{"rule_id": _CLAUDE_GPT_RULE_ID, "ac": "AC1", "paths": [_LIB_SH]}]
    assert risk["verdict"] == "approve"
    assert risk["reasons"] == []
    assert risk["issue_time_exemptions"] == expected
    assert coverage["verdict"] == "approve"
    assert coverage["required_assertions"] == []
    assert coverage["missing"] == []
    assert coverage["issue_time_exemptions"] == expected
    # The raw policy evaluation still reports the (descriptive) hard match.
    assert risk["policy_evaluation"]["final_decision"] == "immediate"


def test_claude_gpt_comment_only_declaration_with_real_canonical_parser():
    """The same fixture driven by the REAL canonical VC parser result
    (`VcParseResult.commands`), proving the duck-typed helper reads real entries."""
    scripts = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from vc_contract_syntax import parse_verification_commands_section

    vc_section = f"```bash\n# AC1\n$ {_GIT_DIFF_LIB_SH}\n```\n"
    parsed = parse_verification_commands_section(vc_section)
    ac_vc_commands = build_ac_vc_commands(parsed.commands)
    assert ac_vc_commands == {"1": (_GIT_DIFF_LIB_SH,)}
    risk, coverage = _evaluate_both([_LIB_SH], _rva(_DECL_AC1), ac_vc_commands=ac_vc_commands)
    assert risk["verdict"] == "approve" and coverage["verdict"] == "approve"


_NEGATIVE_BASE = dict(
    paths=[_LIB_SH],
    rva=_rva(_DECL_AC1),
    ac_section=_AC_SECTION,
    ac_vc_commands=_vc(("AC1", _GIT_DIFF_LIB_SH)),
)


def _case(**overrides):
    return {**_NEGATIVE_BASE, **overrides}


def _predicate_case(path: str):
    """Allowed Path entry `path` plus a `git diff` VC naming the SAME string.

    Every other exemption condition (opt-in, declaration, AC, canonical VC,
    `git diff` shape, path-as-token) is satisfied, so ONLY the exact-file-path
    predicate (`_is_exact_file_path_entry`, In Scope 2) can reject the case.
    Do not inherit `_NEGATIVE_BASE`: its VC names only `lib.sh`, which would let
    the VC check (condition 6) mask a predicate regression."""
    return _case(
        paths=[path],
        ac_vc_commands=_vc(("AC1", f"git diff origin/main -- {path}")),
    )


_NEGATIVE_CASES = {
    "no_declaration": _case(rva=_rva(None)),
    "ac_does_not_exist": _case(
        rva=_rva(f"{{rule: {_CLAUDE_GPT_RULE_ID}, ac: AC9}}"),
        ac_vc_commands=_vc(("AC9", _GIT_DIFF_LIB_SH)),
    ),
    "no_canonical_vc_for_ac": _case(ac_vc_commands=_vc(("AC2", _GIT_DIFF_LIB_SH))),
    "rg_git_diff_is_not_git_diff": _case(
        ac_vc_commands=_vc(("AC1", f"rg 'git diff' {_LIB_SH}"))
    ),
    "echo_git_diff_is_not_git_diff": _case(
        ac_vc_commands=_vc(("AC1", f"echo git diff {_LIB_SH}"))
    ),
    "git_diff_without_the_path": _case(
        ac_vc_commands=_vc(("AC1", "git diff origin/main -- scripts/claude-gpt/other.sh"))
    ),
    "git_status_is_not_git_diff": _case(
        ac_vc_commands=_vc(("AC1", f"git status -- {_LIB_SH}"))
    ),
    "two_exact_paths_only_one_in_diff": _case(
        paths=[_LIB_SH, "scripts/claude-gpt/other.sh"],
    ),
    "glob_allowed_path": _predicate_case("scripts/claude-gpt/**"),
    "glob_allowed_path_star": _predicate_case("scripts/claude-gpt/*"),
    "glob_allowed_path_question": _predicate_case("scripts/claude-gpt/lib?.sh"),
    "glob_allowed_path_bracket": _predicate_case("scripts/claude-gpt/lib[1].sh"),
    "glob_allowed_path_brace": _predicate_case("scripts/claude-gpt/{lib,other}.sh"),
    "directory_allowed_path": _predicate_case("scripts/claude-gpt/"),
    "segment_without_extension": _predicate_case("scripts/claude-gpt/claude-gpt"),
    "segment_without_extension_makefile": _predicate_case("scripts/claude-gpt/Makefile"),
    "git_diff_token_is_option_with_path_not_the_path": _case(
        ac_vc_commands=_vc(("AC1", f"git diff origin/main --output={_LIB_SH}"))
    ),
    "git_diff_token_is_path_with_suffix_not_the_path": _case(
        ac_vc_commands=_vc(("AC1", f"git diff origin/main -- {_LIB_SH}.bak"))
    ),
    "ac_vc_commands_not_provided": _case(ac_vc_commands=None),
    "unterminated_quote_is_not_tokenizable": _case(
        ac_vc_commands=_vc(("AC1", f"git diff 'unterminated {_LIB_SH}"))
    ),
}


@pytest.mark.parametrize("case_id", sorted(_NEGATIVE_CASES))
def test_claude_gpt_comment_only_negative_cases_remain_needs_fix(case_id):
    """AC2: every negative fixture keeps the pre-#2961 hard requirement in BOTH
    shared evaluators (EXTSURF001 / RUNTIMEASSERT001 sides)."""
    case = _NEGATIVE_CASES[case_id]
    risk, coverage = _evaluate_both(
        case["paths"], case["rva"], case["ac_section"], case["ac_vc_commands"]
    )
    assert risk["verdict"] == "needs_fix", case_id
    assert "issue_time_exemptions" not in risk
    assert coverage["verdict"] == "needs_fix", case_id
    assert "issue_time_exemptions" not in coverage
    assert any(_CLAUDE_GPT_PROFILE in item for item in coverage["missing"])


@pytest.mark.parametrize(
    "entry,expected",
    [
        # exact file path (extension-bearing final segment, no glob, no trailing "/")
        ("scripts/claude-gpt/lib.sh", True),
        ("scripts/claude-gpt/other.sh", True),
        ("lib.sh", True),
        ("scripts/claude-gpt/.hidden", True),
        # glob characters, each individually
        ("scripts/claude-gpt/**", False),
        ("scripts/claude-gpt/*", False),
        ("scripts/claude-gpt/*.sh", False),
        ("scripts/claude-gpt/lib?.sh", False),
        ("scripts/claude-gpt/lib[1].sh", False),
        ("scripts/claude-gpt/lib[.sh", False),
        ("scripts/claude-gpt/lib].sh", False),
        ("scripts/claude-gpt/{lib,other}.sh", False),
        ("scripts/claude-gpt/lib{.sh", False),
        ("scripts/claude-gpt/lib}.sh", False),
        # directory / trailing slash
        ("scripts/claude-gpt/", False),
        ("scripts/claude-gpt.d/", False),
        # final segment without "." (no extension)
        ("scripts/claude-gpt/Makefile", False),
        ("scripts/claude-gpt/claude-gpt", False),
        ("scripts.d/claude-gpt", False),
        # degenerate
        ("", False),
        (".", False),
        ("..", False),
        ("scripts/claude-gpt/..", False),
    ],
)
def test_is_exact_file_path_entry_predicate(entry, expected):
    """Direct unit test of the fixed exact-file-path predicate (In Scope 2)."""
    assert extension_surface_policy_matcher._is_exact_file_path_entry(entry) is expected


def test_is_exact_file_path_entry_rejects_non_string():
    assert extension_surface_policy_matcher._is_exact_file_path_entry(None) is False  # type: ignore[arg-type]
    assert extension_surface_policy_matcher._is_exact_file_path_entry(123) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "command,expected",
    [
        (f"git diff origin/main -- {_LIB_SH}", True),
        (f"git diff --stat {_LIB_SH}", True),
        (f"git diff origin/main --output={_LIB_SH}", False),
        (f"git diff origin/main -- {_LIB_SH}.bak", False),
        (f"git diff origin/main -- prefix/{_LIB_SH}", False),
        (f"echo git diff {_LIB_SH}", False),
        (f"rg 'git diff' {_LIB_SH}", False),
        (f"git status -- {_LIB_SH}", False),
        (f"git diff 'unterminated {_LIB_SH}", False),
        ("git diff", False),
    ],
)
def test_command_is_git_diff_for_path_requires_exact_path_token(command, expected):
    """Direct unit test: the path must be a whole token after `git diff`."""
    assert (
        extension_surface_policy_matcher._command_is_git_diff_for_path(command, _LIB_SH)
        is expected
    )


def test_two_exact_paths_each_with_their_own_git_diff_is_exempted():
    """Positive counterpart: when EVERY exact path has a `git diff` VC naming it
    (possibly in different commands of the declared AC) the exemption applies."""
    other = "scripts/claude-gpt/other.sh"
    ac_vc_commands = _vc(
        ("AC1", _GIT_DIFF_LIB_SH), ("AC1", f"git diff --stat origin/main -- {other}")
    )
    risk, coverage = _evaluate_both([_LIB_SH, other], _rva(_DECL_AC1), ac_vc_commands=ac_vc_commands)
    assert risk["verdict"] == "approve" and coverage["verdict"] == "approve"
    assert risk["issue_time_exemptions"][0]["paths"] == sorted([_LIB_SH, other])


def test_exemption_does_not_remove_another_matched_rules_hard_requirement():
    """AC2 (other rule matches simultaneously): the claude-gpt rule alone is
    excluded; the agent rule's hard `immediate` requirement stays."""
    risk, coverage = _evaluate_both(
        [_LIB_SH, ".claude/agents/implementation-worker.md"],
        _rva(_DECL_AC1),
        ac_vc_commands=_vc(("AC1", _GIT_DIFF_LIB_SH)),
    )
    assert risk["verdict"] == "needs_fix"
    assert "agent-lifecycle-frontmatter-or-body-change" in risk["reasons"][0]
    assert _CLAUDE_GPT_RULE_ID not in risk["reasons"][0]
    assert risk["issue_time_exemptions"][0]["rule_id"] == _CLAUDE_GPT_RULE_ID
    assert coverage["verdict"] == "needs_fix"
    assert not any(_CLAUDE_GPT_PROFILE in item for item in coverage["missing"])
    assert any("agent" in item for item in coverage["missing"])


@pytest.mark.parametrize(
    "declaration",
    [
        f"{{rule: {_CLAUDE_GPT_RULE_ID}, ac: AC1, extra: x}}",
        f"{{rule: {_CLAUDE_GPT_RULE_ID}}}",
        "{ac: AC1}",
        f"{{rule: {_CLAUDE_GPT_RULE_ID}, ac: 1}}",
        "{rule: '', ac: AC1}",
        "AC1",
        f"[{{rule: {_CLAUDE_GPT_RULE_ID}, ac: AC1}}]",
    ],
)
def test_malformed_declaration_is_not_applied(declaration):
    """The declaration is a closed key set (`rule` / `ac`); anything else is not applied."""
    risk, coverage = _evaluate_both(
        [_LIB_SH], _rva(declaration), ac_vc_commands=_vc(("AC1", _GIT_DIFF_LIB_SH))
    )
    assert risk["verdict"] == "needs_fix" and coverage["verdict"] == "needs_fix"
    assert risk["issue_time_exemption_rejections"]


@pytest.mark.parametrize(
    "paths,rule_id",
    [
        ([".claude/agents/implementation-worker.md"], "agent-lifecycle-frontmatter-or-body-change"),
        ([".claude/hooks/some_hook.py"], "hook-lifecycle-matcher-or-handler-change"),
        ([".claude/skills/foo/SKILL.md"], "skill-invocation-procedure-or-contract-change"),
        ([".claude/agents/implementation-worker.md"], "subagent-lifecycle-start-stop-delegation-fallback"),
        ([".claude/hooks/some_hook.py"], _CLAUDE_GPT_RULE_ID),
    ],
)
def test_exemption_declaration_does_not_exempt_hook_or_skill_rule(paths, rule_id):
    """AC7: hook / skill / subagent / agent rules (and the claude-gpt rule when
    it is not even matched) never get the exemption, whatever is declared."""
    path = paths[0]
    risk, coverage = _evaluate_both(
        paths,
        _rva(f"{{rule: {rule_id}, ac: AC1}}"),
        ac_vc_commands=_vc(("AC1", f"git diff origin/main -- {path}")),
    )
    assert risk["verdict"] == "needs_fix"
    assert "issue_time_exemptions" not in risk
    assert coverage["verdict"] == "needs_fix"
    assert "issue_time_exemptions" not in coverage


def test_only_claude_gpt_rule_opts_in_in_the_policy_file():
    policy = load_policy()
    opted_in = [r["id"] for r in policy["rules"] if "issue_time_exemption" in r]
    assert opted_in == [_CLAUDE_GPT_RULE_ID]


def test_unsupported_policy_opt_in_fails_closed_with_policy_load_error():
    policy = load_policy()
    for rule in policy["rules"]:
        if rule["id"] == _CLAUDE_GPT_RULE_ID:
            rule["issue_time_exemption"] = {"declaration_key": "other", "mode": "x"}
    with pytest.raises(extension_surface_policy_matcher.PolicyLoadError):
        evaluate_issue_risk_trigger(
            [_LIB_SH],
            "not_applicable",
            _rva(_DECL_AC1),
            policy=policy,
            ac_section_text=_AC_SECTION,
            ac_vc_commands=_vc(("AC1", _GIT_DIFF_LIB_SH)),
        )


_VALID_LOOKING_OPT_IN = {
    "declaration_key": "executable_semantics_unchanged",
    "mode": "exact_file_path_git_diff_vc",
}


def _evaluate_with_mutated_policy(mutate, paths, rule_id):
    """Pass a mutated policy dict DIRECTLY to both decision-critical evaluators."""
    path = paths[0]
    kwargs = dict(
        ac_section_text=_AC_SECTION,
        ac_vc_commands=_vc(("AC1", f"git diff origin/main -- {path}")),
    )
    rva = _rva(f"{{rule: {rule_id}, ac: AC1}}")
    policy = load_policy()
    mutate(policy)
    risk = evaluate_issue_risk_trigger(paths, "not_applicable", rva, policy=policy, **kwargs)
    policy = load_policy()
    mutate(policy)
    coverage = evaluate_runtime_assertion_binding_coverage(
        paths,
        rva,
        _AC_SECTION,
        {"1"},
        policy=policy,
        ac_vc_commands=kwargs["ac_vc_commands"],
    )
    return risk, coverage


@pytest.mark.parametrize(
    "paths,rule_id",
    [
        ([".claude/agents/implementation-worker.md"], "agent-lifecycle-frontmatter-or-body-change"),
        ([".claude/hooks/some_hook.py"], "hook-lifecycle-matcher-or-handler-change"),
        ([".claude/skills/foo/SKILL.md"], "skill-invocation-procedure-or-contract-change"),
        ([".claude/agents/implementation-worker.md"], "subagent-lifecycle-start-stop-delegation-fallback"),
    ],
)
def test_runtime_matcher_rejects_exemption_copied_to_non_claude_gpt_rule(paths, rule_id):
    """OWNER review PR #2965 Finding 2: a valid-looking opt-in copied to any
    non-claude-gpt rule must fail closed in the runtime matcher itself
    (mutated policy passed directly to both decision-critical evaluators)."""
    assert rule_id != _CLAUDE_GPT_RULE_ID
    assert any(r["id"] == rule_id for r in load_policy()["rules"])

    def mutate(policy):
        for rule in policy["rules"]:
            if rule["id"] == rule_id:
                rule["issue_time_exemption"] = dict(_VALID_LOOKING_OPT_IN)

    with pytest.raises(extension_surface_policy_matcher.PolicyLoadError):
        _evaluate_with_mutated_policy(mutate, paths, rule_id)


def test_runtime_matcher_rejects_extra_key_in_claude_gpt_opt_in():
    def mutate(policy):
        for rule in policy["rules"]:
            if rule["id"] == _CLAUDE_GPT_RULE_ID:
                rule["issue_time_exemption"] = {**_VALID_LOOKING_OPT_IN, "extra": True}

    with pytest.raises(extension_surface_policy_matcher.PolicyLoadError):
        _evaluate_with_mutated_policy(mutate, [_LIB_SH], _CLAUDE_GPT_RULE_ID)


def test_validate_policy_contract_rejects_misplaced_or_extra_key_opt_in(tmp_path):
    for target, value in (
        ("hook", dict(_VALID_LOOKING_OPT_IN)),
        (_CLAUDE_GPT_RULE_ID, {**_VALID_LOOKING_OPT_IN, "extra": 1}),
    ):
        data = load_policy()
        for rule in data["rules"]:
            if target == _CLAUDE_GPT_RULE_ID and rule["id"] == target:
                rule["issue_time_exemption"] = value
            elif target == "hook" and rule["id"] != _CLAUDE_GPT_RULE_ID:
                rule["issue_time_exemption"] = value
                break
        with pytest.raises(extension_surface_policy_matcher.PolicyLoadError):
            extension_surface_policy_matcher._validate_policy_contract(data, tmp_path / "p.yaml")


def test_old_call_signature_is_unchanged_and_never_exempts():
    """Callers that do not pass the new optional arguments keep the previous behaviour."""
    risk = evaluate_issue_risk_trigger([_LIB_SH], "not_applicable", _rva(_DECL_AC1))
    assert risk["verdict"] == "needs_fix"
    assert "issue_time_exemptions" not in risk
    coverage = evaluate_runtime_assertion_binding_coverage(
        [_LIB_SH], _rva(_DECL_AC1), _AC_SECTION, {"1"}
    )
    assert coverage["verdict"] == "needs_fix"


def test_exemption_does_not_weaken_an_immediate_declaration():
    """A declared `immediate` Issue is evaluated exactly as before (the exemption
    only removes the rule from the final-decision derivation)."""
    risk = evaluate_issue_risk_trigger(
        [_LIB_SH],
        "immediate",
        "- decision: immediate\n- reason: x",
        ac_section_text=_AC_SECTION,
        ac_vc_commands=_vc(("AC1", _GIT_DIFF_LIB_SH)),
    )
    assert risk["verdict"] == "needs_fix"
    assert risk["missing_rva_immediate_fields"]


def test_build_ac_vc_commands_is_a_pure_duck_typed_mapping():
    entries = [
        _FakeVcEntry({"AC1", "AC2"}, "cmd-a"),
        _FakeVcEntry({"AC1"}, "cmd-b"),
        _FakeVcEntry(set(), "unlabelled"),
        _FakeVcEntry({"not-an-ac"}, "bogus"),
    ]
    assert build_ac_vc_commands(entries) == {"1": ("cmd-a", "cmd-b"), "2": ("cmd-a",)}
    assert build_ac_vc_commands(None) == {}
    assert build_ac_vc_commands([]) == {}


def test_format_issue_time_exemption_lines_is_one_line_per_exemption():
    lines = format_issue_time_exemption_lines(
        [{"rule_id": _CLAUDE_GPT_RULE_ID, "ac": "AC1", "paths": [_LIB_SH]}]
    )
    assert lines == [
        f"{_CLAUDE_GPT_RULE_ID}: exempted via executable_semantics_unchanged; "
        f"ac=AC1; paths={_LIB_SH}"
    ]
