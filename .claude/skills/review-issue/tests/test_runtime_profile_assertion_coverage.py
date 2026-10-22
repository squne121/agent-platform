"""Tests for C15 (`check_c15_runtime_assertion_binding_coverage`, Issue #2771).

Covers AC3 (canonical `runtime_assertion_bindings` wire format parses
deterministically from the Runtime Verification Applicability section) and
AC4 (missing / unknown / duplicate composite-identity reason codes, and the
"1 key = 1 ac" rule: a duplicated key is invalid regardless of whether its
bound `ac` values match).

All fixtures use the real production policy
(`docs/dev/extension-surface-runtime-policy.yaml`) via real Allowed Path
matches:

- `.claude/hooks/**` -> hook-chain-runtime-smoke (hard, 2 assertions,
  no overlap with any other rule's path_globs)
- `scripts/claude-gpt/**` -> claude-gpt-process-io-smoke (hard, 1 assertion,
  no overlap with any other rule's path_globs)
- `.claude/skills/**/scripts/**` -> skill-invocation-runtime-smoke, but only
  its *advisory* selector (the `.claude/skills/**/SKILL.md` selector of the
  same rule is hard -- deliberately not used here to keep the advisory-only
  fixtures unambiguous)

`.claude/agents/**` and `.claude/skills/**/SKILL.md` are deliberately NOT
used here: both also match the (unrelated) `subagent-lifecycle-start-stop-
delegation-fallback` rule's `path_globs`, which would pull in a second
hard-required profile as an unintended side effect of the fixture's Allowed
Paths choice. Using `.claude/hooks/**` / `scripts/claude-gpt/**` keeps each
single-profile fixture's required set unambiguous.

This test exercises the real production policy shape end-to-end (not a
synthetic fixture policy -- that lower-level exercise already lives in
`scripts/agent-guards/tests/test_runtime_profile_assertion_coverage.py`).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
import check_issue_contract as checker  # noqa: E402


_BASE_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for C15 fixture testing purposes only.

## Acceptance Criteria

{ac_section}

## Verification Commands

```bash
{vc_section}
```

## Allowed Paths

{allowed_paths}

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
{applicable_acs}
execution_environment:
  cli_tools:
    - python3
skip_conditions:
  - "none"
fallback_policy:
  fallback_success_is_pass: false
artifact_requirements:
  - "artifacts/out.json"
{runtime_assertion_bindings}
```

## Required Skills

none
"""


def _ac_line(n: int, text: str) -> str:
    return f"- [ ] AC{n}: {text} <!-- runtime-verification: true -->"


def _vc_line(n: int) -> str:
    return f"# AC{n}\n$ rg -n 'concrete' file{n}.py"


def _build_body(
    *,
    allowed_paths: str,
    ac_numbers: list[int],
    applicable_ac_numbers: list[int],
    runtime_assertion_bindings_yaml: str,
) -> str:
    ac_section = "\n".join(_ac_line(n, f"concrete AC {n}") for n in ac_numbers)
    vc_section = "\n".join(_vc_line(n) for n in ac_numbers)
    applicable_acs = "\n".join(f"  - AC{n}" for n in applicable_ac_numbers)
    return _BASE_BODY_TEMPLATE.format(
        ac_section=ac_section,
        vc_section=vc_section,
        allowed_paths=allowed_paths,
        applicable_acs=applicable_acs,
        runtime_assertion_bindings=runtime_assertion_bindings_yaml,
    )


_HOOK_ASSERTION_1 = "all_matching_hooks_observed"
_HOOK_ASSERTION_2 = "sibling_side_effect_inventory_complete"
_CLAUDE_GPT_ASSERTION = "external_process_exit_code_and_stdio_observed"


# --- AC3: canonical wire format parses deterministically -----------------


def test_hard_required_profile_with_no_bindings_declared_is_missing():
    """GIVEN a hard-matched profile (hook-chain-runtime-smoke, 2 assertions)
    and no runtime_assertion_bindings declared at all
    WHEN C15 evaluates
    THEN it FAILs with a 'missing' reason for both assertions."""
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1],
        applicable_ac_numbers=[1],
        runtime_assertion_bindings_yaml="",
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.FAIL
    assert any("missing" in issue for issue in issues)
    assert any(_HOOK_ASSERTION_1 in issue for issue in issues)
    assert any(_HOOK_ASSERTION_2 in issue for issue in issues)


def test_partial_binding_2765_style_is_missing():
    """GIVEN a hard-matched profile requiring 2 assertions but only 1 bound
    (the #2765 defect shape)
    WHEN C15 evaluates
    THEN it FAILs with a 'missing' reason for the unbound assertion only."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
    )
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1],
        applicable_ac_numbers=[1],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.FAIL
    missing_lines = [issue for issue in issues if issue.startswith("missing runtime_assertion_bindings")]
    assert len(missing_lines) == 1
    assert _HOOK_ASSERTION_2 in missing_lines[0]
    assert _HOOK_ASSERTION_1 not in missing_lines[0]


def test_complete_single_profile_binding_passes():
    """GIVEN a hard-matched single profile with both assertions bound to
    valid, referentially-consistent ACs
    WHEN C15 evaluates
    THEN it PASSes (AC8: valid fixtures do not regress)."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_2}\n"
        "    ac: AC2\n"
    )
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1, 2],
        applicable_ac_numbers=[1, 2],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.PASS
    assert issues == []


def test_complete_multiple_profiles_simultaneously_passes():
    """GIVEN two hard-matched profiles at once (hooks + claude-gpt), each
    with all assertions bound
    WHEN C15 evaluates
    THEN it PASSes (AC8: multi-profile valid fixture, Issue #2467 precedent)."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_2}\n"
        "    ac: AC2\n"
        "  - profile: claude-gpt-process-io-smoke\n"
        f"    assertion: {_CLAUDE_GPT_ASSERTION}\n"
        "    ac: AC3\n"
    )
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py\n- scripts/claude-gpt/bar.py",
        ac_numbers=[1, 2, 3],
        applicable_ac_numbers=[1, 2, 3],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.PASS
    assert issues == []


# --- AC1 (body-level companion): advisory-only match needs no bindings ---


def test_advisory_only_match_with_no_bindings_passes():
    """GIVEN an Allowed Path that only matches an advisory selector
    (`.claude/skills/**/scripts/**`) and no runtime_assertion_bindings
    declared
    WHEN C15 evaluates
    THEN it PASSes -- advisory-only matches never require bindings."""
    body = _build_body(
        allowed_paths="- .claude/skills/foo/scripts/bar.py",
        ac_numbers=[1],
        applicable_ac_numbers=[1],
        runtime_assertion_bindings_yaml="",
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.PASS
    assert issues == []


def test_advisory_only_match_with_declared_binding_is_unknown():
    """GIVEN an advisory-only match (no hard-required set at all) but the
    author declares a runtime_assertion_bindings entry anyway
    WHEN C15 evaluates
    THEN it FAILs with an 'unknown' reason (the declared pair is not
    hard-required, AC4)."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: skill-invocation-runtime-smoke\n"
        "    assertion: procedure_steps_executed_in_declared_order\n"
        "    ac: AC1\n"
    )
    body = _build_body(
        allowed_paths="- .claude/skills/foo/scripts/bar.py",
        ac_numbers=[1],
        applicable_ac_numbers=[1],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.FAIL
    assert any("not hard-required" in issue for issue in issues)


# --- AC4: composite identity missing / unknown / duplicate ---------------


def test_unknown_binding_for_unrelated_profile_is_reported():
    """GIVEN a hard-required profile whose bindings are all complete, plus
    one extra declared binding for a profile that was never matched at all
    WHEN C15 evaluates
    THEN it FAILs with an 'unknown' reason for the extraneous pair."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_2}\n"
        "    ac: AC2\n"
        "  - profile: claude-gpt-process-io-smoke\n"
        f"    assertion: {_CLAUDE_GPT_ASSERTION}\n"
        "    ac: AC1\n"
    )
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1, 2],
        applicable_ac_numbers=[1, 2],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.FAIL
    assert any("not hard-required" in issue and "claude-gpt-process-io-smoke" in issue for issue in issues)


def test_duplicate_key_with_different_ac_is_reported_as_duplicate():
    """GIVEN the same (profile, assertion) key declared twice with two
    *different* `ac` targets
    WHEN C15 evaluates
    THEN it FAILs with a 'duplicate' reason -- 1 key = 1 ac; bind-target
    identity of the duplicate does not matter (AC4)."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC2\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_2}\n"
        "    ac: AC2\n"
    )
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1, 2],
        applicable_ac_numbers=[1, 2],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.FAIL
    assert any("duplicate" in issue for issue in issues)


def test_duplicate_key_with_same_ac_is_still_reported_as_duplicate():
    """GIVEN the same (profile, assertion) key declared twice with the
    *same* `ac` target both times
    WHEN C15 evaluates
    THEN it still FAILs with a 'duplicate' reason (AC4: duplicate keying is
    invalid regardless of whether the bound ac matches)."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_2}\n"
        "    ac: AC2\n"
    )
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1, 2],
        applicable_ac_numbers=[1, 2],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.FAIL
    assert any("duplicate" in issue for issue in issues)


def test_multiple_distinct_bindings_may_share_the_same_ac():
    """GIVEN two distinct (profile, assertion) keys that both bind to the
    *same* ac (one AC/VC evidencing two assertions at once -- the #2775
    escape valve shape)
    WHEN C15 evaluates
    THEN it PASSes -- sharing an ac across distinct keys is explicitly
    allowed (AC4), only same-key duplication is invalid."""
    bindings_yaml = (
        "runtime_assertion_bindings:\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_1}\n"
        "    ac: AC1\n"
        "  - profile: hook-chain-runtime-smoke\n"
        f"    assertion: {_HOOK_ASSERTION_2}\n"
        "    ac: AC1\n"
    )
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1],
        applicable_ac_numbers=[1],
        runtime_assertion_bindings_yaml=bindings_yaml,
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert status == checker.CheckResult.PASS
    assert issues == []


def test_research_kind_issue_is_not_applicable():
    """Non-implementation issue_kind is not applicable to C15 (parity with
    C14's existing issue_kind guard)."""
    body = _build_body(
        allowed_paths="- .claude/hooks/foo.py",
        ac_numbers=[1],
        applicable_ac_numbers=[1],
        runtime_assertion_bindings_yaml="",
    )
    status, issues = checker.check_c15_runtime_assertion_binding_coverage(body, "research")
    assert status == checker.CheckResult.NA
    assert issues == []
