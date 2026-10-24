"""
Regression tests for VC annotation support in check_issue_contract.py.

Covers:
- # preflight-scope: pr_review_only / runtime_only → structured skipped result
- unknown # preflight-scope: value → fail-closed / human_judgment
- # trivially_pass: <reason> → skipped, not unexpected_pass
- annotation comments themselves are NOT extracted as commands
- annotation invalidated when blank line or non-annotation comment appears between annotation and command
- rg -n "createComment" broad grep → regression fixture demonstrating context-fixed VC is needed
- --json stdout/stderr contract not broken (#574/#598 compat)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# ---------------------------------------------------------------------------
# Helper: directly import parse_vc_commands + ParsedVcCommand + PreflightScope
# from the script without relying on subprocess. We add the scripts dir to sys.path.
# ---------------------------------------------------------------------------
import importlib.util
import os
import sys as _sys

# Register the module under its canonical name before exec to fix dataclass __module__ resolution
_spec = importlib.util.spec_from_file_location("check_issue_contract", str(SCRIPT_PATH))
_mod = importlib.util.module_from_spec(_spec)
_sys.modules["check_issue_contract"] = _mod
_spec.loader.exec_module(_mod)

parse_vc_commands = _mod.parse_vc_commands
ParsedVcCommand = _mod.ParsedVcCommand
PreflightScope = _mod.PreflightScope


# ---------------------------------------------------------------------------
# Helper: run the script via subprocess for --json contract tests
# ---------------------------------------------------------------------------

def _run_checker_json(body: str) -> dict:
    """Run check_issue_contract.py --json on an in-memory body and return parsed JSON."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        tmp_path = f.name
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--file", tmp_path, "--json"],
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 1), (
            f"Script exited {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return json.loads(result.stdout)
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Test: preflight-scope: pr_review_only
# ---------------------------------------------------------------------------

class TestPreflightScopePrReviewOnly:
    """GIVEN a VC block with # preflight-scope: pr_review_only above a command
    WHEN parse_vc_commands runs
    THEN the command is classified as skipped with skip_reason_type preflight_scope."""

    def test_pr_review_only_classification_skipped(self):
        """AC2: pr_review_only produces classification=skipped."""
        vc_section = """
```bash
# preflight-scope: pr_review_only
$ rg -n "createComment" .claude/skills/
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        cmd = commands[0]
        assert cmd.classification == "skipped"

    def test_pr_review_only_preflight_scope_value(self):
        """AC1: preflight_scope metadata is preserved as PreflightScope.PR_REVIEW_ONLY."""
        vc_section = """
```bash
# preflight-scope: pr_review_only
$ rg -n "createComment" .claude/skills/
```
"""
        commands = parse_vc_commands(vc_section)
        assert commands[0].preflight_scope == PreflightScope.PR_REVIEW_ONLY

    def test_pr_review_only_skip_reason_type(self):
        """AC2: skip_reason_type is preflight_scope for pr_review_only."""
        vc_section = """
```bash
# preflight-scope: pr_review_only
$ rg -n "createComment" .claude/skills/
```
"""
        commands = parse_vc_commands(vc_section)
        assert commands[0].skip_reason_type == "preflight_scope"


# ---------------------------------------------------------------------------
# Test: preflight-scope: runtime_only
# ---------------------------------------------------------------------------

class TestPreflightScopeRuntimeOnly:
    """GIVEN a VC block with # preflight-scope: runtime_only above a command
    WHEN parse_vc_commands runs
    THEN the command is classified as skipped with PreflightScope.RUNTIME_ONLY."""

    def test_runtime_only_classification_skipped(self):
        """AC2: runtime_only produces classification=skipped."""
        vc_section = """
```bash
# preflight-scope: runtime_only
$ uv run pytest .claude/skills/review-issue/tests/ -q
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        assert commands[0].classification == "skipped"

    def test_runtime_only_preflight_scope_value(self):
        """AC1+AC2: preflight_scope metadata is PreflightScope.RUNTIME_ONLY."""
        vc_section = """
```bash
# preflight-scope: runtime_only
$ uv run pytest .claude/skills/review-issue/tests/ -q
```
"""
        commands = parse_vc_commands(vc_section)
        assert commands[0].preflight_scope == PreflightScope.RUNTIME_ONLY


# ---------------------------------------------------------------------------
# Test: unknown preflight-scope value → fail-closed / human_judgment
# ---------------------------------------------------------------------------

class TestUnknownPreflightScope:
    """GIVEN a VC block with # preflight-scope: <unknown_value> above a command
    WHEN parse_vc_commands runs
    THEN the command is fail-closed: classification=skipped, skip_reason_type=preflight_scope_human_judgment."""

    def test_unknown_preflight_scope_classification_skipped(self):
        """AC3: unknown preflight-scope produces classification=skipped (fail-closed)."""
        vc_section = """
```bash
# preflight-scope: not_a_known_value
$ rg -n "something" src/
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        assert commands[0].classification == "skipped"

    def test_unknown_preflight_scope_is_unknown_enum(self):
        """AC3: unknown preflight-scope uses PreflightScope.UNKNOWN."""
        vc_section = """
```bash
# preflight-scope: not_a_known_value
$ rg -n "something" src/
```
"""
        commands = parse_vc_commands(vc_section)
        assert commands[0].preflight_scope == PreflightScope.UNKNOWN

    def test_unknown_preflight_scope_human_judgment_reason(self):
        """AC3: skip_reason_type is preflight_scope_human_judgment for unknown values."""
        vc_section = """
```bash
# preflight-scope: not_a_known_value
$ rg -n "something" src/
```
"""
        commands = parse_vc_commands(vc_section)
        assert commands[0].skip_reason_type == "preflight_scope_human_judgment"


# ---------------------------------------------------------------------------
# Test: trivially_pass annotation
# ---------------------------------------------------------------------------

class TestTriviallyPassAnnotation:
    """GIVEN a VC block with # trivially_pass: <reason> above a command
    WHEN parse_vc_commands runs
    THEN the command is classified=skipped with skip_reason_type=trivially_pass
    and the reason is preserved (non-empty)."""

    def test_trivially_pass_classification_skipped(self):
        """AC4: trivially_pass produces classification=skipped."""
        vc_section = """
```bash
# trivially_pass: This check is a no-op on a fresh repo
$ git diff --check
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        assert commands[0].classification == "skipped"

    def test_trivially_pass_reason_preserved(self):
        """AC4: trivially_pass reason is stored as trivially_pass_reason (non-empty)."""
        vc_section = """
```bash
# trivially_pass: This check is a no-op on a fresh repo
$ git diff --check
```
"""
        commands = parse_vc_commands(vc_section)
        reason = commands[0].trivially_pass_reason
        assert reason is not None
        assert len(reason) > 0
        assert "no-op" in reason

    def test_trivially_pass_skip_reason_type(self):
        """AC4: skip_reason_type is trivially_pass."""
        vc_section = """
```bash
# trivially_pass: always true because file is immutable
$ test -f pyproject.toml
```
"""
        commands = parse_vc_commands(vc_section)
        assert commands[0].skip_reason_type == "trivially_pass"

    def test_trivially_pass_not_unexpected_pass(self):
        """AC4: trivially_pass command must not be classified as unexpected_pass.
        Also verifies trivially_pass_reason is non-empty and skip_reason_type is correct."""
        vc_section = """
```bash
# trivially_pass: already exists in baseline
$ rg 'foo' bar.py
```
"""
        commands = parse_vc_commands(vc_section)
        cmd = commands[0]
        assert cmd.classification == "skipped"
        assert cmd.skip_reason_type == "trivially_pass"
        assert cmd.trivially_pass_reason is not None
        assert cmd.trivially_pass_reason != ""
        assert cmd.trivially_pass_reason == "already exists in baseline"
        # Core of B4: must not be classified as unexpected_pass
        assert cmd.classification != "unexpected_pass"


# ---------------------------------------------------------------------------
# Test: annotation comment NOT extracted as command (AC5)
# ---------------------------------------------------------------------------

class TestAnnotationNotExecutedAsCommand:
    """GIVEN a VC block containing # preflight-scope: / # trivially_pass: annotation lines
    WHEN parse_vc_commands runs
    THEN annotation comment lines are NOT returned as ParsedVcCommand entries."""

    def test_annotation_not_executed_as_command_preflight_scope(self):
        """AC5: # preflight-scope: line is not returned as a command."""
        vc_section = """
```bash
# preflight-scope: pr_review_only
$ rg -n "createComment" .claude/
```
"""
        commands = parse_vc_commands(vc_section)
        # Should only return the actual $ command, not the annotation line
        assert len(commands) == 1
        assert commands[0].command.startswith("$")
        # annotation text must not appear as a command
        for cmd in commands:
            assert "preflight-scope" not in cmd.command

    def test_annotation_not_executed_as_command_trivially_pass(self):
        """AC5: # trivially_pass: line is not returned as a command."""
        vc_section = """
```bash
# trivially_pass: always succeeds
$ pnpm typecheck
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        assert commands[0].command.startswith("$")
        for cmd in commands:
            assert "trivially_pass" not in cmd.command

    def test_only_dollar_lines_are_commands(self):
        """AC5: only lines starting with $ are extracted as commands."""
        vc_section = """
```bash
# preflight-scope: runtime_only
$ uv run pytest tests/ -q
# trivially_pass: static check
$ pnpm lint
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 2
        for cmd in commands:
            assert cmd.command.startswith("$")


# ---------------------------------------------------------------------------
# Test: annotation out-of-range (blank line between annotation and command)
# ---------------------------------------------------------------------------

class TestAnnotationOutOfRangeIgnored:
    """GIVEN a VC block where a blank line appears between annotation and command
    WHEN parse_vc_commands runs
    THEN the annotation is invalidated (command extracted with no annotation metadata)."""

    def test_annotation_out_of_range_ignored_blank_line(self):
        """AC6: blank line between annotation and command invalidates annotation."""
        vc_section = """
```bash
# preflight-scope: pr_review_only

$ rg -n "createComment" .claude/
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        # annotation should be None (invalidated by blank line)
        assert commands[0].preflight_scope is None
        assert commands[0].classification == "executable"

    def test_annotation_out_of_range_ignored_intervening_comment(self):
        """AC6: non-annotation comment between annotation and command invalidates annotation."""
        vc_section = """
```bash
# preflight-scope: pr_review_only
# some other comment
$ rg -n "createComment" .claude/
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        # The intervening non-annotation comment should reset the annotation
        assert commands[0].preflight_scope is None
        assert commands[0].classification == "executable"

    def test_direct_annotation_above_command_valid(self):
        """AC6: annotation directly above command (no blank lines) is valid."""
        vc_section = """
```bash
# preflight-scope: pr_review_only
$ rg -n "createComment" .claude/
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        assert commands[0].preflight_scope == PreflightScope.PR_REVIEW_ONLY
        assert commands[0].classification == "skipped"


# ---------------------------------------------------------------------------
# Test: rg -n "createComment" broad grep regression fixture (AC7)
# ---------------------------------------------------------------------------

class TestCreateCommentNoBroadFalsePositive:
    """GIVEN a VC with `rg -n "createComment"` as a broad grep (no path restriction)
    WHEN it is NOT annotated with context-fixed scope
    THEN it should be classified as executable (no false positive skipping)
    AND the regression fixture demonstrates that annotation is needed for scope control.

    AC7: demonstrates that broad grep requires context-fixed annotation to be skipped;
    without annotation, it is treated as a regular executable command."""

    def test_createComment_no_annotation_is_executable(self):
        """AC7: rg -n createComment without annotation → classification=executable (no false positive)."""
        vc_section = """
```bash
$ rg -n "createComment" .claude/skills/
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        # Without annotation, broad grep is plain executable — no false positive
        assert commands[0].classification == "executable"
        assert commands[0].preflight_scope is None
        assert commands[0].trivially_pass_reason is None

    def test_createComment_with_pr_review_only_is_skipped(self):
        """AC7: rg -n createComment WITH pr_review_only annotation → skipped (context-fixed)."""
        vc_section = """
```bash
# preflight-scope: pr_review_only
$ rg -n "createComment" .claude/skills/
```
"""
        commands = parse_vc_commands(vc_section)
        assert len(commands) == 1
        assert commands[0].classification == "skipped"
        assert commands[0].preflight_scope == PreflightScope.PR_REVIEW_ONLY

    def test_createComment_no_false_positive(self):
        """AC7: regression fixture — broad grep without annotation must NOT be auto-skipped.
        Annotation is the explicit mechanism; no heuristic auto-skipping of grep patterns."""
        vc_section = """
```bash
$ rg -n "createComment" .claude/
$ rg -n "createComment" src/
$ grep -r "createComment" .
```
"""
        commands = parse_vc_commands(vc_section)
        # All three commands have no annotation → all should be executable
        assert len(commands) == 3
        for cmd in commands:
            assert cmd.classification == "executable", (
                f"Expected executable for unannotated grep, got {cmd.classification} for: {cmd.command}"
            )


# ---------------------------------------------------------------------------
# Test: --json stdout/stderr contract not broken (AC8)
# ---------------------------------------------------------------------------

# Minimal pass-worthy issue body for testing JSON contract stability
_MINIMAL_PASS_ISSUE = """\
---
TITLE: 実装: minimal pass issue for json contract test
LABELS: phase/implementation
---
## Outcome

foo.py が bar を出力する。

## In Scope

- foo.py に bar 出力を追加する

## Acceptance Criteria

- [ ] AC1: foo.py を実行すると bar が標準出力に出る

## Verification Commands

```bash
# AC1
$ python foo.py | grep bar
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- スコープ外対応が必要になった場合
- ライセンス判断が必要な変更が発生した場合
- 別 Issue の作成が必要な問題が発覚した場合
- 既存テストの大規模改変が必要な場合
- 外部サービス依存が発覚した場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
reason: "unit test のみで完結する"
```

## Allowed Paths

- foo.py

## Required Skills

- Python
"""


class TestJsonContractNotBroken:
    """GIVEN an issue body WHEN check_issue_contract.py --json runs
    THEN stdout is valid JSON with the expected top-level keys (#574/#598 contract)."""

    def test_json_output_is_valid_json(self):
        """AC8: --json produces valid JSON on stdout."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        # Just reaching here means json.loads succeeded
        assert isinstance(output, dict)

    def test_json_has_verdict_key(self):
        """AC8: JSON output contains 'verdict' key."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert "verdict" in output

    def test_json_has_issue_kind_key(self):
        """AC8: JSON output contains 'issue_kind' key."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert "issue_kind" in output

    def test_json_has_generated_at_key(self):
        """AC8: JSON output contains 'generated_at' key."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert "generated_at" in output

    def test_json_has_deterministic_checks_key(self):
        """AC8: JSON output contains 'deterministic_checks' key."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert "deterministic_checks" in output

    def test_json_deterministic_checks_has_13_keys(self):
        """AC8: deterministic_checks has exactly 13 keys (C1-C13, unchanged)."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        checks = output["deterministic_checks"]
        assert len(checks) == 13, (
            f"Expected 13 checks, got {len(checks)}: {list(checks.keys())}"
        )

    def test_json_has_blocking_issues_key(self):
        """AC8: JSON output contains 'blocking_issues' key (list)."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert "blocking_issues" in output
        assert isinstance(output["blocking_issues"], list)

    def test_json_has_non_blocking_improvements_key(self):
        """AC8: JSON output contains 'non_blocking_improvements' key (list)."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert "non_blocking_improvements" in output
        assert isinstance(output["non_blocking_improvements"], list)

    def test_json_has_diff_proposal_key(self):
        """AC8: JSON output contains 'diff_proposal' key (dict)."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert "diff_proposal" in output
        assert isinstance(output["diff_proposal"], dict)

    def test_json_verdict_is_string(self):
        """AC8: verdict is a string value (approve or needs-fix)."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert isinstance(output["verdict"], str)
        assert output["verdict"] in ("approve", "needs-fix")

    def test_json_no_stdout_pollution(self):
        """AC8: --json mode emits only JSON to stdout (no debug/info lines mixed in)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(_MINIMAL_PASS_ISSUE)
            tmp_path = f.name
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--file", tmp_path, "--json"],
                capture_output=True,
                text=True,
            )
            # stdout must be parseable as JSON from the very first character
            stdout = result.stdout.strip()
            assert stdout.startswith("{"), (
                f"stdout does not start with '{{': {stdout[:100]}"
            )
            parsed = json.loads(stdout)
            assert isinstance(parsed, dict)
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Test: --json output contains parsed_vc_commands (B2 fix — E2E connection test)
# ---------------------------------------------------------------------------

class TestJsonOutputContainsParsedVcCommands:
    """GIVEN a VC section with annotation
    WHEN check_issue_contract.py --json --file runs
    THEN the JSON output contains a 'parsed_vc_commands' field with annotation metadata."""

    def test_json_output_has_parsed_vc_commands_key(self):
        """B2: --json output must include 'parsed_vc_commands' key (E2E connection test)."""
        issue_body = _MINIMAL_PASS_ISSUE.replace(
            "```bash\n# AC1\n$ python foo.py | grep bar\n```",
            "```bash\n# AC1\n# preflight-scope: pr_review_only\n$ rg -n \"foo\" bar.py\n```",
        )
        output = _run_checker_json(issue_body)
        assert "parsed_vc_commands" in output, (
            f"'parsed_vc_commands' key missing from --json output. Keys present: {list(output.keys())}"
        )

    def test_json_parsed_vc_commands_is_list(self):
        """B2: parsed_vc_commands must be a list in --json output."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        assert isinstance(output.get("parsed_vc_commands"), list)

    def test_json_parsed_vc_commands_preflight_scope_annotation(self):
        """B2: parsed_vc_commands entries include preflight_scope annotation metadata."""
        issue_body = _MINIMAL_PASS_ISSUE.replace(
            "```bash\n# AC1\n$ python foo.py | grep bar\n```",
            "```bash\n# AC1\n# preflight-scope: pr_review_only\n$ rg -n \"foo\" bar.py\n```",
        )
        output = _run_checker_json(issue_body)
        cmds = output["parsed_vc_commands"]
        assert len(cmds) >= 1
        annotated = [c for c in cmds if c.get("preflight_scope") == "pr_review_only"]
        assert len(annotated) >= 1, (
            f"Expected at least one command with preflight_scope=pr_review_only, got: {cmds}"
        )
        cmd = annotated[0]
        assert cmd["classification"] == "skipped"
        assert cmd["skip_reason_type"] == "preflight_scope"

    def test_json_parsed_vc_commands_trivially_pass_annotation(self):
        """B2+B4: parsed_vc_commands entries include trivially_pass_reason (non-empty) via E2E path."""
        issue_body = _MINIMAL_PASS_ISSUE.replace(
            "```bash\n# AC1\n$ python foo.py | grep bar\n```",
            "```bash\n# AC1\n# trivially_pass: already validated in prior iteration\n$ python foo.py | grep bar\n```",
        )
        output = _run_checker_json(issue_body)
        cmds = output["parsed_vc_commands"]
        assert len(cmds) >= 1
        tp_cmds = [c for c in cmds if c.get("skip_reason_type") == "trivially_pass"]
        assert len(tp_cmds) >= 1, (
            f"Expected at least one command with skip_reason_type=trivially_pass, got: {cmds}"
        )
        cmd = tp_cmds[0]
        assert cmd["classification"] == "skipped"
        assert cmd["trivially_pass_reason"] is not None
        assert cmd["trivially_pass_reason"] != ""
        assert cmd["trivially_pass_reason"] == "already validated in prior iteration"

    def test_json_unannotated_command_is_executable(self):
        """B2: unannotated commands appear in parsed_vc_commands with classification=executable."""
        output = _run_checker_json(_MINIMAL_PASS_ISSUE)
        cmds = output["parsed_vc_commands"]
        assert len(cmds) >= 1
        executable = [c for c in cmds if c.get("classification") == "executable"]
        assert len(executable) >= 1, (
            f"Expected at least one executable command, got: {cmds}"
        )


# ---------------------------------------------------------------------------
# Standalone test function targeted by VC: -k "json_contract_not_broken"
# Note: pytest -k parses "not" as a keyword operator. Using a standalone function
# avoids the class name "NotBroken" matching issue.
# ---------------------------------------------------------------------------

def test_json_contract_not_broken_standalone():
    """AC8: standalone regression — --json contract keys are all present and unchanged.

    This function is named so that `pytest -k json_contract_not_broken` works correctly
    (pytest parses 'not' in -k expressions as a boolean operator; a standalone function
    with the literal name avoids that ambiguity).
    """
    output = _run_checker_json(_MINIMAL_PASS_ISSUE)
    required_keys = {
        "verdict",
        "issue_kind",
        "generated_at",
        "deterministic_checks",
        "blocking_issues",
        "non_blocking_improvements",
        "diff_proposal",
        "parsed_vc_commands",
    }
    missing = required_keys - set(output.keys())
    assert not missing, f"Missing keys in --json output: {missing}"
    assert len(output["deterministic_checks"]) == 13, (
        f"Expected 13 deterministic checks, got {len(output['deterministic_checks'])}"
    )
