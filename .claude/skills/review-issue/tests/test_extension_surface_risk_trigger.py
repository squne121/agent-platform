"""Tests for C14 (`check_c14_extension_surface_risk_trigger`, Issue #2290).

Covers AC6: `decision: immediate` declared but the Runtime Verification
Applicability contract's required immediate fields are missing ->
needs-fix (CheckResult.FAIL).
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

Concrete outcome sentence for C14 fixture testing purposes only.

## Acceptance Criteria

- [ ] AC1: concrete AC
<!-- runtime-verification: true -->

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
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

{rva_section}

## Required Skills

none
"""


def test_risk_trigger_immediate_incomplete_returns_needs_fix():
    """AC6: decision: immediate but required immediate fields are missing -> FAIL."""
    body = _BASE_BODY_TEMPLATE.format(
        allowed_paths="- .claude/agents/implementation-worker.md",
        rva_section=(
            "- decision: immediate\n"
            "- reason: risky agent definition change requires runtime smoke test"
        ),
    )
    status, issues = checker.check_c14_extension_surface_risk_trigger(body, "implementation")
    assert status == checker.CheckResult.FAIL
    assert issues
    assert any("missing" in issue for issue in issues)


def test_risk_trigger_immediate_complete_with_matching_scope_passes():
    """decision: immediate with all required fields present and Allowed Paths
    matching a risky selector -> PASS (declared decision already at/above
    the policy's most-restrictive requirement, and RVA immediate fields
    are complete)."""
    rva_section = (
        "- decision: immediate\n"
        "applicable_acs: [AC1]\n"
        "execution_environment: worktree-agent-runtime-smoke\n"
        "skip_conditions: none\n"
        "fallback_policy: escalate_to_human\n"
        "artifact_requirements: artifacts/smoke.json\n"
    )
    body = _BASE_BODY_TEMPLATE.format(
        allowed_paths="- .claude/agents/implementation-worker.md",
        rva_section=rva_section,
    )
    status, issues = checker.check_c14_extension_surface_risk_trigger(body, "implementation")
    assert status == checker.CheckResult.PASS
    assert issues == []


def test_risk_trigger_docs_only_not_applicable_passes():
    """Non-implementation issue_kind is not applicable to C14."""
    body = _BASE_BODY_TEMPLATE.format(
        allowed_paths="- docs/dev/foo.md",
        rva_section="- decision: not_applicable\n- reason: docs only",
    )
    status, issues = checker.check_c14_extension_surface_risk_trigger(body, "research")
    assert status == checker.CheckResult.NA
    assert issues == []


# ---------------------------------------------------------------------------
# Issue #2961: comment-only `scripts/claude-gpt/**` exemption (consumer side)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

_CLAUDE_GPT_RULE_ID = "claude-gpt-lifecycle-invocation-change"
_LIB_SH = "scripts/claude-gpt/lib.sh"
_EXEMPTION_CODE = "extension_surface_issue_time_exemption_applied"
_GIT_DIFF_VC = f"git diff origin/main -- {_LIB_SH}"

_COMMENT_ONLY_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "comment-only exemption fixture"
change_kind: workflow
```

## Parent Issue

none

## Outcome

`scripts/claude-gpt/lib.sh` の comment を現行 semantics に合わせて修正する。

## Parent Goal Ref

- Goal: comment-only 変更の readiness false blocker 解消
- Desired Destination: 単独改善

## Current Validated Scope

- `scripts/claude-gpt/lib.sh` の comment のみを変更する

## Remaining Parent Gaps

なし

## In Scope

- `scripts/claude-gpt/lib.sh` の comment 修正

## Out of Scope

- 実行時挙動の変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `lib.sh` の変更が comment 行のみである
- [ ] AC2: `lib.sh` の shell 構文が壊れていない

## Verification Commands

```bash
{vc_lines}
# AC2
$ bash -n scripts/claude-gpt/lib.sh
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

- decision: not_applicable
- reason: comment のみの変更で実行時挙動は変わらない
{declaration}

## Allowed Paths

{allowed_paths}
"""

_DECLARATION_AC1 = f"- executable_semantics_unchanged: {{rule: {_CLAUDE_GPT_RULE_ID}, ac: AC1}}"


def _comment_only_body(
    *,
    allowed_paths: str = f"- {_LIB_SH}",
    declaration: str = _DECLARATION_AC1,
    vc_lines: str = f"# AC1\n$ {_GIT_DIFF_VC}",
) -> str:
    return _COMMENT_ONLY_BODY_TEMPLATE.format(
        allowed_paths=allowed_paths, declaration=declaration, vc_lines=vc_lines
    )


def _predicate_negative(path: str) -> dict:
    return dict(
        allowed_paths=f"- {path}",
        vc_lines=f"# AC1\n$ git diff origin/main -- {path}",
    )


_COMMENT_ONLY_NEGATIVES = {
    "no_declaration": dict(declaration=""),
    "ac_does_not_exist": dict(
        declaration=f"- executable_semantics_unchanged: {{rule: {_CLAUDE_GPT_RULE_ID}, ac: AC9}}",
        vc_lines=f"# AC9\n$ {_GIT_DIFF_VC}",
    ),
    "no_canonical_vc_for_ac": dict(vc_lines=f"# AC3\n$ {_GIT_DIFF_VC}"),
    "rg_git_diff": dict(vc_lines=f"# AC1\n$ rg 'git diff' {_LIB_SH}"),
    "echo_git_diff": dict(vc_lines=f"# AC1\n$ echo git diff {_LIB_SH}"),
    "git_diff_without_path": dict(
        vc_lines="# AC1\n$ git diff origin/main -- scripts/claude-gpt/other.sh"
    ),
    "two_exact_paths_one_diffed": dict(
        allowed_paths=f"- {_LIB_SH}\n- scripts/claude-gpt/other.sh"
    ),
    # Predicate-only negatives: the AC1 `git diff` VC names the SAME string as the
    # Allowed Path entry, so only the exact-file-path predicate can reject them.
    "glob_path": _predicate_negative("scripts/claude-gpt/**"),
    "glob_path_star": _predicate_negative("scripts/claude-gpt/*"),
    "glob_path_question": _predicate_negative("scripts/claude-gpt/lib?.sh"),
    "directory_path": _predicate_negative("scripts/claude-gpt/"),
    "segment_without_extension": _predicate_negative("scripts/claude-gpt/claude-gpt"),
    "segment_without_extension_makefile": _predicate_negative("scripts/claude-gpt/Makefile"),
    # path-in-tokens strictness: the path must be a whole token of the git diff VC.
    "git_diff_option_embeds_path": dict(
        vc_lines=f"# AC1\n$ git diff origin/main --output={_LIB_SH}"
    ),
    "git_diff_path_with_suffix": dict(
        vc_lines=f"# AC1\n$ git diff origin/main -- {_LIB_SH}.bak"
    ),
    "other_rule_also_matches": dict(
        allowed_paths=f"- {_LIB_SH}\n- .claude/agents/implementation-worker.md"
    ),
}


def test_comment_only_claude_gpt_no_extsurf001():
    """AC3: the #2956-shaped fixture raises neither C14 (EXTSURF001) nor C15
    (RUNTIMEASSERT001), the applied exemption is carried into
    `non_blocking_improvements`, and the verdict is unchanged (approve)."""
    body = _comment_only_body()
    assert checker.check_c14_extension_surface_risk_trigger(body, "implementation") == (
        checker.CheckResult.PASS,
        [],
    )
    assert checker.check_c15_runtime_assertion_binding_coverage(body, "implementation") == (
        checker.CheckResult.PASS,
        [],
    )

    result = checker.result_to_dict(
        checker.run_checks(
            body, labels="phase/implementation,kind/implementation", title="実装: comment-only"
        )
    )
    assert result["verdict"] == "approve"
    assert result["blocking_issues"] == []
    carriers = [e for e in result["non_blocking_improvements"] if e["code"] == _EXEMPTION_CODE]
    assert len(carriers) == 1
    assert carriers[0]["severity"] == "advisory"
    assert carriers[0]["evidence"] == [
        f"{_CLAUDE_GPT_RULE_ID}: exempted via executable_semantics_unchanged; "
        f"ac=AC1; paths={_LIB_SH}"
    ]
    assert _EXEMPTION_CODE in checker._NON_BLOCKING_READINESS_ERROR_CATEGORIES

    # Verdict unchanged by the carrier: the only difference from a body whose
    # declaration is not applied is the needs-fix verdict of THAT body.
    assert checker.get_extension_surface_issue_time_exemption_carrier(body, "implementation")


@pytest.mark.parametrize("case_id", sorted(_COMMENT_ONLY_NEGATIVES))
def test_comment_only_claude_gpt_negatives_stay_needs_fix(case_id):
    """AC3: each AC2-style negative still yields C14 / C15 FAIL and a needs-fix
    verdict, and never carries an exemption."""
    body = _comment_only_body(**_COMMENT_ONLY_NEGATIVES[case_id])
    c14, _ = checker.check_c14_extension_surface_risk_trigger(body, "implementation")
    c15, _ = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert c14 == checker.CheckResult.FAIL, case_id
    assert c15 == checker.CheckResult.FAIL, case_id
    carrier = checker.get_extension_surface_issue_time_exemption_carrier(body, "implementation")
    if case_id == "other_rule_also_matches":
        # Only the claude-gpt rule is exempted (reported as info); the other
        # matched rule keeps its hard requirement, so the verdict stays needs-fix.
        assert len(carrier) == 1
    else:
        assert carrier == []
    result = checker.result_to_dict(
        checker.run_checks(
            body, labels="phase/implementation,kind/implementation", title="実装: comment-only"
        )
    )
    assert result["verdict"] == "needs-fix"
    if case_id != "other_rule_also_matches":
        assert not [e for e in result["non_blocking_improvements"] if e["code"] == _EXEMPTION_CODE]


class _RecordingEvaluator:
    """Proxy over the real shared evaluator that records how the consumer
    transports VC command bodies (the ONLY consumer-side behaviour of #2961)."""

    def __init__(self, real):
        self._real = real
        self.build_calls = []
        self.risk_calls = []
        self.coverage_calls = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def build_ac_vc_commands(self, commands):
        self.build_calls.append(list(commands))
        return self._real.build_ac_vc_commands(commands)

    def evaluate_issue_risk_trigger(self, **kwargs):
        self.risk_calls.append(kwargs)
        return self._real.evaluate_issue_risk_trigger(**kwargs)

    def evaluate_runtime_assertion_binding_coverage(self, **kwargs):
        self.coverage_calls.append(kwargs)
        return self._real.evaluate_runtime_assertion_binding_coverage(**kwargs)


def _install_recorder(monkeypatch):
    real = checker._load_extension_surface_policy_matcher()
    recorder = _RecordingEvaluator(real)
    monkeypatch.setattr(checker, "_load_extension_surface_policy_matcher", lambda: recorder)
    return recorder


def test_vc_command_transport_shared_helper_and_parser_fallback(monkeypatch):
    """AC8: with the canonical parser, review-issue builds `ac_vc_commands` with
    the SHARED `build_ac_vc_commands` helper and passes the same mapping to BOTH
    call sites (C14 / C15), each also receiving `ac_section_text`; in the
    `_VC_SECTION_PARSER_AVAILABLE == False` regex-fallback path nothing is passed
    (`None`), so no exemption is ever applied."""
    body = _comment_only_body()

    recorder = _install_recorder(monkeypatch)
    assert checker.check_c14_extension_surface_risk_trigger(body, "implementation")[0] == (
        checker.CheckResult.PASS
    )
    assert checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")[0] == (
        checker.CheckResult.PASS
    )
    expected = {"1": (_GIT_DIFF_VC,), "2": ("bash -n scripts/claude-gpt/lib.sh",)}
    assert len(recorder.risk_calls) == 1 and len(recorder.coverage_calls) == 1
    assert recorder.risk_calls[0]["ac_vc_commands"] == expected
    assert recorder.coverage_calls[0]["ac_vc_commands"] == expected
    assert "AC1" in recorder.risk_calls[0]["ac_section_text"]
    assert "AC1" in recorder.coverage_calls[0]["ac_section_text"]
    assert len(recorder.build_calls) >= 2  # both call sites go through the shared helper

    # Parser unavailable (regex fallback): `ac_vc_commands` is NOT passed.
    monkeypatch.setattr(checker, "_VC_SECTION_PARSER_AVAILABLE", False)
    fallback_recorder = _install_recorder(monkeypatch)
    c14, _ = checker.check_c14_extension_surface_risk_trigger(body, "implementation")
    c15, _ = checker.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert fallback_recorder.risk_calls[0]["ac_vc_commands"] is None
    assert fallback_recorder.coverage_calls[0]["ac_vc_commands"] is None
    assert fallback_recorder.build_calls == []
    assert c14 == checker.CheckResult.FAIL
    assert c15 == checker.CheckResult.FAIL
    assert checker.get_extension_surface_issue_time_exemption_carrier(body, "implementation") == []


def test_merge_readiness_dedupes_the_exemption_carrier_with_the_review_carrier():
    """The readiness-side info carrier is routed to `non_blocking_improvements`
    (never a blocker) and a byte-identical review-side entry is not duplicated."""
    evidence = [
        f"{_CLAUDE_GPT_RULE_ID}: exempted via executable_semantics_unchanged; "
        f"ac=AC1; paths={_LIB_SH}"
    ]
    body = _comment_only_body()
    review_result = checker.result_to_dict(
        checker.run_checks(
            body, labels="phase/implementation,kind/implementation", title="実装: comment-only"
        )
    )
    assert [e["code"] for e in review_result["non_blocking_improvements"]].count(_EXEMPTION_CODE) == 1
    readiness_result = {
        "status": "go",
        "body_sha256": review_result["body_sha256"],
        "errors": [
            {
                "rule_id": "EXTSURF004",
                "severity": "info",
                "category": _EXEMPTION_CODE,
                "minimal_context": evidence,
                "fix_hint": "Non-blocking",
            }
        ],
    }
    merged = checker.merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="contract_readiness_check_result",
        iteration_id="iter-1",
    )
    assert merged["verdict"] == "approve"
    assert merged["structured_blockers"] == []
    assert [e["code"] for e in merged["non_blocking_improvements"]].count(_EXEMPTION_CODE) == 1
