"""P1-1 fix delta parity test (Issue #2290, PR #2335 OWNER review).

`.claude/skills/review-issue/scripts/check_issue_contract.py`'s
`check_c14_extension_surface_risk_trigger()` guards on
``issue_kind != "implementation"`` (returns NA). Before this fix delta,
`.claude/skills/issue-contract-review/scripts/contract_readiness_check.py`'s
`check_extension_surface_risk_trigger()` had no equivalent guard and would
flag a research-kind Issue whose declared Allowed Paths happen to overlap an
extension-surface selector, breaking cross-consumer parity (Issue #2290 AC7
requirement). This test fixes a research-kind Issue with a risky
(``.claude/agents/**``) Allowed Path entry and asserts both consumers agree
on not-applicable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CONTRACT_READINESS_SCRIPTS = _HERE.parent / "scripts"


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_RESEARCH_KIND_RISKY_ALLOWED_PATH_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: "none"
goal_ref: "extension surface parity fixture"
change_kind: workflow
```

## Outcome

Research-kind fixture for extension-surface risk-trigger parity testing only.

## Acceptance Criteria

- [ ] AC1: research fixture AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- .claude/agents/foo.md

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: research fixture, no runtime execution needed

## Required Skills

none
"""


def test_research_kind_issue_is_not_applicable_on_both_consumers():
    check_issue_contract = _load_module_from_path(
        "check_issue_contract_for_ext_surface_p11_parity_test",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_ext_surface_p11_parity_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _RESEARCH_KIND_RISKY_ALLOWED_PATH_BODY, "research"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(
        _RESEARCH_KIND_RISKY_ALLOWED_PATH_BODY
    )

    # review-issue: NA, no reasons.
    assert review_issue_status == check_issue_contract.CheckResult.NA
    assert review_issue_reasons == []

    # issue-contract-review: empty error list (not-applicable equivalent).
    assert readiness_errors == []


# ---------------------------------------------------------------------------
# Issue #2961: comment-only `scripts/claude-gpt/**` exemption parity
# (review-issue C14/C15 vs issue-contract-review EXTSURF001/RUNTIMEASSERT001)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

_CLAUDE_GPT_RULE_ID = "claude-gpt-lifecycle-invocation-change"
_LIB_SH = "scripts/claude-gpt/lib.sh"
_EXEMPTION_CATEGORY = "extension_surface_issue_time_exemption_applied"
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



_CLOSING_HASH_VARIANTS = {
    "plain_heading": lambda body: body,
    "gfm_closing_hash_heading": lambda body: body.replace(
        "## Verification Commands\n", "## Verification Commands ##\n"
    ),
}


def _load_pair(suffix: str):
    cic = _load_module_from_path(
        f"check_issue_contract_for_2961_{suffix}", _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py"
    )
    crc = _load_module_from_path(
        f"contract_readiness_check_for_2961_{suffix}",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )
    return cic, crc


def _readiness_categories(crc, body: str) -> set[str]:
    errors = crc.check_extension_surface_risk_trigger(body) + crc.check_runtime_assertion_binding_coverage(
        body
    )
    return {e["rule_id"] for e in errors}


@pytest.mark.parametrize("variant", sorted(_CLOSING_HASH_VARIANTS))
def test_comment_only_claude_gpt_parity(variant):
    """AC4: for the AC1 fixture (plain and GFM closing-hash `## Verification
    Commands ##` headings) issue-contract-review returns the same verdict as
    review-issue -- no EXTSURF001 / RUNTIMEASSERT001, status go -- and exposes the
    SAME `issue_time_exemptions` content as the info-severity category
    `extension_surface_issue_time_exemption_applied`."""
    cic, crc = _load_pair(f"parity_{variant}")
    body = _CLOSING_HASH_VARIANTS[variant](_comment_only_body())
    assert ("## Verification Commands ##" in body) == (variant == "gfm_closing_hash_heading")

    c14, _ = cic.check_c14_extension_surface_risk_trigger(body, "implementation")
    c15, _ = cic.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert c14 == c15 == cic.CheckResult.PASS
    assert _readiness_categories(crc, body) == set()

    review_lines = cic.get_extension_surface_issue_time_exemption_carrier(body, "implementation")
    readiness_errors = crc.check_extension_surface_issue_time_exemption(body)
    assert len(readiness_errors) == 1
    carrier = readiness_errors[0]
    assert carrier["rule_id"] == "EXTSURF004"
    assert carrier["severity"] == "info"
    assert carrier["category"] == _EXEMPTION_CATEGORY
    assert carrier["autofixable"] is False
    assert review_lines == carrier["minimal_context"] == [
        f"{_CLAUDE_GPT_RULE_ID}: exempted via executable_semantics_unchanged; "
        f"ac=AC1; paths={_LIB_SH}"
    ]

    review = cic.result_to_dict(
        cic.run_checks(body, labels="phase/implementation,kind/implementation", title="実装: x")
    )
    entry = next(e for e in review["non_blocking_improvements"] if e["code"] == _EXEMPTION_CATEGORY)
    assert entry["evidence"] == carrier["minimal_context"]
    assert review["verdict"] == "approve"

    result = crc.build_result(body, "static", crc.run_validate_issue_body(body), None, None)
    categories = [e["category"] for e in result["errors"]]
    assert _EXEMPTION_CATEGORY in categories
    assert "extension_surface_risk_trigger" not in categories
    assert "runtime_assertion_binding_coverage" not in categories
    if variant == "plain_heading":
        # (The unrelated LP001 body_lint check does not recognise a GFM
        # closing-hash heading, so full-status `go` is asserted on the plain form.)
        assert result["status"] == "go"
        assert result["fix_hint"] is None and result["minimal_context"] == []
        assert categories == [_EXEMPTION_CATEGORY]
    assert _EXEMPTION_CATEGORY in crc._NON_BLOCKING_READINESS_ERROR_CATEGORIES
    assert _EXEMPTION_CATEGORY in cic._NON_BLOCKING_READINESS_ERROR_CATEGORIES


@pytest.mark.parametrize("case_id", sorted(_COMMENT_ONLY_NEGATIVES))
def test_comment_only_claude_gpt_negatives_parity(case_id):
    """AC4 (negative side): every negative fixture is needs_fix on BOTH consumers."""
    cic, crc = _load_pair(f"neg_{case_id}")
    body = _comment_only_body(**_COMMENT_ONLY_NEGATIVES[case_id])
    c14, _ = cic.check_c14_extension_surface_risk_trigger(body, "implementation")
    c15, _ = cic.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert c14 == c15 == cic.CheckResult.FAIL, case_id
    assert _readiness_categories(crc, body) == {"EXTSURF001", "RUNTIMEASSERT001"}, case_id
    result = crc.build_result(body, "static", crc.run_validate_issue_body(body), None, None)
    assert result["status"] == "needs_fix", case_id


class _RecordingEvaluator:
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


@pytest.mark.parametrize("variant", sorted(_CLOSING_HASH_VARIANTS))
def test_vc_command_transport_readiness_call_sites(monkeypatch, variant):
    """AC8 (readiness side): both readiness call sites (EXTSURF001 and
    RUNTIMEASSERT001) receive the SAME `ac_vc_commands` mapping, built by the
    shared `build_ac_vc_commands` helper, and the same mapping review-issue
    passes for the same body (including a GFM closing-hash VC heading)."""
    cic, crc = _load_pair(f"transport_{variant}")
    body = _CLOSING_HASH_VARIANTS[variant](_comment_only_body())

    crc_recorder = _RecordingEvaluator(crc._load_extension_surface_policy_matcher())
    monkeypatch.setattr(crc, "_load_extension_surface_policy_matcher", lambda: crc_recorder)
    cic_recorder = _RecordingEvaluator(cic._load_extension_surface_policy_matcher())
    monkeypatch.setattr(cic, "_load_extension_surface_policy_matcher", lambda: cic_recorder)

    assert crc.check_extension_surface_risk_trigger(body) == []
    assert crc.check_runtime_assertion_binding_coverage(body) == []
    cic.check_c14_extension_surface_risk_trigger(body, "implementation")
    cic.check_c15_runtime_assertion_binding_coverage(body, "implementation")

    expected = {"1": (_GIT_DIFF_VC,), "2": ("bash -n scripts/claude-gpt/lib.sh",)}
    assert len(crc_recorder.risk_calls) == 1 and len(crc_recorder.coverage_calls) == 1
    assert crc_recorder.risk_calls[0]["ac_vc_commands"] == expected
    assert crc_recorder.coverage_calls[0]["ac_vc_commands"] == expected
    assert cic_recorder.risk_calls[0]["ac_vc_commands"] == expected
    assert cic_recorder.coverage_calls[0]["ac_vc_commands"] == expected
    assert "AC1" in crc_recorder.risk_calls[0]["ac_section_text"]
    assert "AC1" in crc_recorder.coverage_calls[0]["ac_section_text"]
    assert len(crc_recorder.build_calls) >= 2  # both readiness call sites use the shared helper
