#!/usr/bin/env python3
"""
Tests for VC grammar parity across 3 checkers (Issue #993).

AC5: canonical fixture は 3 checker すべて pass
AC6: non-canonical fixtures は 3 checker すべて fail
AC7: grouped AC marker / inline suffix は 3 checker の合否が一致

3 checkers:
  - check_issue_contract.py (C4/C5)
  - validate_issue_body.py (LP010/LP011/LP016)
  - baseline_vc_preflight.py (--static-only)
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path


# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent
# __file__ is at: <repo>/.claude/skills/review-issue/tests/test_vc_grammar_parity.py
# parents: [0]=tests, [1]=review-issue, [2]=skills, [3]=.claude, [4]=<repo root>
_REPO_ROOT = Path(__file__).resolve().parents[4]

_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CREATE_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
_CONTRACT_REVIEW_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"

CHECK_CONTRACT_SCRIPT = _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py"
VALIDATE_BODY_SCRIPT = _CREATE_ISSUE_SCRIPTS / "validate_issue_body.py"
PREFLIGHT_SCRIPT = _CONTRACT_REVIEW_SCRIPTS / "baseline_vc_preflight.py"

FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "issue-body"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_check_contract(body: str) -> dict:
    """Run check_issue_contract.py and return parsed JSON output."""
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        path = f.name
    try:
        result = subprocess.run(
            [sys.executable, str(CHECK_CONTRACT_SCRIPT), "--file", path, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout, f"No output from check_contract: stderr={result.stderr[:500]}"
        return json.loads(result.stdout)
    finally:
        os.unlink(path)


def _run_validate_body(body: str) -> dict:
    """Run validate_issue_body.py and return parsed JSON output."""
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        path = f.name
    try:
        result = subprocess.run(
            [sys.executable, str(VALIDATE_BODY_SCRIPT), "--body-file", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout, f"No output from validate_body: stderr={result.stderr[:500]}"
        return json.loads(result.stdout)
    finally:
        os.unlink(path)


def _run_preflight_static(body: str) -> dict:
    """Run baseline_vc_preflight.py --static-only and return parsed JSON output."""
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        path = f.name
    try:
        result = subprocess.run(
            [sys.executable, str(PREFLIGHT_SCRIPT), "--body-file", path, "--static-only"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout, f"No output from preflight: stderr={result.stderr[:500]}"
        return json.loads(result.stdout)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Fixture body definitions
# ---------------------------------------------------------------------------

_CANONICAL_BODY = textwrap.dedent("""\
    ## Acceptance Criteria

    - [ ] AC1: コマンド 1
    - [ ] AC2: コマンド 2

    ## Verification Commands

    ```bash
    # AC1
    $ uv run pytest tests/ -x -q

    # AC2
    $ uv run pytest tests/fixtures/ -q
    ```

    ## Allowed Paths

    - tests/fixtures/issue-body/

    ## Stop Conditions

    - In Scope 外の変更が必要と判明した場合
    - Allowed Paths 外の変更が必要と判明した場合
    - 依存サービスが利用不可の場合
    - テストが 3 回以上失敗し続ける場合
    - データ整合性の問題が発生した場合
    - セキュリティ上の懸念が発生した場合

    ## Runtime Verification Applicability

    decision: not_applicable
""")

_COLON_MARKER_BODY = textwrap.dedent("""\
    ## Acceptance Criteria

    - [ ] AC1: コマンド 1

    ## Verification Commands

    ```bash
    # AC1: コマンド 1 の説明
    $ uv run pytest tests/ -x -q
    ```

    ## Allowed Paths

    - tests/fixtures/issue-body/

    ## Stop Conditions

    - In Scope 外の変更が必要と判明した場合
    - Allowed Paths 外の変更が必要と判明した場合
    - 依存サービスが利用不可の場合
    - テストが 3 回以上失敗し続ける場合
    - データ整合性の問題が発生した場合
    - セキュリティ上の懸念が発生した場合

    ## Runtime Verification Applicability

    decision: not_applicable
""")

_INLINE_BACKTICK_BODY = textwrap.dedent("""\
    ## Acceptance Criteria

    - [ ] AC1: コマンド 1

    ## Verification Commands

    - `$ uv run pytest tests/ -x -q`

    ## Allowed Paths

    - tests/fixtures/issue-body/

    ## Stop Conditions

    - In Scope 外の変更が必要と判明した場合
    - Allowed Paths 外の変更が必要と判明した場合
    - 依存サービスが利用不可の場合
    - テストが 3 回以上失敗し続ける場合
    - データ整合性の問題が発生した場合
    - セキュリティ上の懸念が発生した場合

    ## Runtime Verification Applicability

    decision: not_applicable
""")

_UNLABELED_FENCE_BODY = textwrap.dedent("""\
    ## Acceptance Criteria

    - [ ] AC1: コマンド 1

    ## Verification Commands

    ```
    # AC1
    $ uv run pytest tests/ -x -q
    ```

    ## Allowed Paths

    - tests/fixtures/issue-body/

    ## Stop Conditions

    - In Scope 外の変更が必要と判明した場合
    - Allowed Paths 外の変更が必要と判明した場合
    - 依存サービスが利用不可の場合
    - テストが 3 回以上失敗し続ける場合
    - データ整合性の問題が発生した場合
    - セキュリティ上の懸念が発生した場合

    ## Runtime Verification Applicability

    decision: not_applicable
""")

_GROUPED_MARKER_BODY = textwrap.dedent("""\
    ## Acceptance Criteria

    - [ ] AC1: コマンド 1
    - [ ] AC2: コマンド 2
    - [ ] AC3: コマンド 3

    ## Verification Commands

    ```bash
    # AC1
    $ uv run pytest tests/ -x -q

    # AC2, AC3
    $ uv run pytest tests/fixtures/ -q
    ```

    ## Allowed Paths

    - tests/fixtures/issue-body/

    ## Stop Conditions

    - In Scope 外の変更が必要と判明した場合
    - Allowed Paths 外の変更が必要と判明した場合
    - 依存サービスが利用不可の場合
    - テストが 3 回以上失敗し続ける場合
    - データ整合性の問題が発生した場合
    - セキュリティ上の懸念が発生した場合

    ## Runtime Verification Applicability

    decision: not_applicable
""")

_INLINE_SUFFIX_BODY = textwrap.dedent("""\
    ## Acceptance Criteria

    - [ ] AC1: コマンド 1

    ## Verification Commands

    ```bash
    $ uv run pytest tests/ -x -q # AC1
    ```

    ## Allowed Paths

    - tests/fixtures/issue-body/

    ## Stop Conditions

    - In Scope 外の変更が必要と判明した場合
    - Allowed Paths 外の変更が必要と判明した場合
    - 依存サービスが利用不可の場合
    - テストが 3 回以上失敗し続ける場合
    - データ整合性の問題が発生した場合
    - セキュリティ上の懸念が発生した場合

    ## Runtime Verification Applicability

    decision: not_applicable
""")


# ---------------------------------------------------------------------------
# AC5: canonical fixture は 3 checker すべて pass
# ---------------------------------------------------------------------------

class TestAC5CanonicalPass:
    """AC5: canonical VC format は C4/C5 pass, LP016 なし, preflight static ok."""

    def test_check_contract_c4_pass(self):
        """C4 must pass for canonical VC."""
        output = _run_check_contract(_CANONICAL_BODY)
        c4 = output["deterministic_checks"]["C4_vc_commands_present"]
        assert c4 == "pass", f"C4 should be pass for canonical VC, got {c4!r}"

    def test_check_contract_c5_pass(self):
        """C5 must pass for canonical VC with all ACs covered."""
        output = _run_check_contract(_CANONICAL_BODY)
        c5 = output["deterministic_checks"]["C5_ac_vc_number_alignment"]
        assert c5 == "pass", f"C5 should be pass for canonical VC, got {c5!r}"

    def test_validate_body_lp011_no_error(self):
        """LP011 must not fire for canonical VC with bash fence."""
        output = _run_validate_body(_CANONICAL_BODY)
        lp011 = [e for e in output["errors"] if e["rule_id"] == "LP011"]
        assert len(lp011) == 0, f"LP011 should not fire for canonical VC: {lp011}"

    def test_validate_body_lp016_no_error(self):
        """LP016 must not fire for canonical bare AC markers."""
        output = _run_validate_body(_CANONICAL_BODY)
        lp016 = [e for e in output["errors"] if e["rule_id"] == "LP016"]
        assert len(lp016) == 0, f"LP016 should not fire for bare AC markers: {lp016}"

    def test_preflight_static_ok(self):
        """Preflight --static-only must return status=ok for canonical VC."""
        output = _run_preflight_static(_CANONICAL_BODY)
        assert output["status"] == "ok", (
            f"Preflight static should be ok for canonical VC, got {output['status']!r}"
        )


# ---------------------------------------------------------------------------
# AC6: non-canonical fixtures は 3 checker すべて fail
# ---------------------------------------------------------------------------

class TestAC6ColonMarkerFail:
    """AC6: colon marker は LP016 fail + C5 fail (marker not recognized) + preflight static blocked."""

    def test_validate_body_lp016_fires(self):
        """LP016 must fire for colon AC markers."""
        output = _run_validate_body(_COLON_MARKER_BODY)
        lp016 = [e for e in output["errors"] if e["rule_id"] == "LP016"]
        assert len(lp016) >= 1, "LP016 should fire for '# AC1:' marker"
        assert "bare" in lp016[0]["message"], f"LP016 message should mention 'bare': {lp016[0]['message']}"

    def test_validate_body_lp016_colon_fix_hint(self):
        """LP016 must have colon-specific fix_hint for colon markers."""
        output = _run_validate_body(_COLON_MARKER_BODY)
        lp016 = [e for e in output["errors"] if e["rule_id"] == "LP016"]
        assert len(lp016) >= 1
        # colon-specific fix_hint should mention removing the colon
        hint = lp016[0]["fix_hint"]
        assert "colon" in hint.lower() or ":" in hint, (
            f"LP016 fix_hint should be colon-specific for '# AC1:' marker: {hint!r}"
        )

    def test_check_contract_c5_fail(self):
        """C5 must fail for colon marker (AC not recognized in VC refs)."""
        output = _run_check_contract(_COLON_MARKER_BODY)
        c5 = output["deterministic_checks"]["C5_ac_vc_number_alignment"]
        assert c5 == "fail", (
            f"C5 should fail for colon marker (AC1 not recognized), got {c5!r}"
        )

    def test_preflight_static_blocked(self):
        """Preflight --static-only must return blocked for colon AC markers."""
        output = _run_preflight_static(_COLON_MARKER_BODY)
        assert output["status"] == "blocked", (
            f"Preflight static should be blocked for colon marker, got {output['status']!r}"
        )


class TestAC6InlineBacktickFail:
    """AC6: inline backtick VC は C4 fail + LP011 fail + preflight static blocked."""

    def test_check_contract_c4_fail(self):
        """C4 must fail for inline backtick VC (no bash fence + no $ commands)."""
        output = _run_check_contract(_INLINE_BACKTICK_BODY)
        c4 = output["deterministic_checks"]["C4_vc_commands_present"]
        assert c4 == "fail", (
            f"C4 should fail for inline backtick VC (no canonical commands), got {c4!r}"
        )

    def test_validate_body_lp011_fires(self):
        """LP011 must fire for inline backtick VC (no bash fence)."""
        output = _run_validate_body(_INLINE_BACKTICK_BODY)
        lp011 = [e for e in output["errors"] if e["rule_id"] == "LP011"]
        assert len(lp011) >= 1, "LP011 should fire for inline backtick VC"

    def test_preflight_static_result(self):
        """Preflight --static-only must be blocked for inline backtick VC."""
        output = _run_preflight_static(_INLINE_BACKTICK_BODY)
        assert output.get("status") == "blocked", (
            f"Preflight should be blocked for inline backtick VC, got status={output.get('status')!r}"
        )
        all_error_kinds = [
            err.get("kind")
            for r in output.get("results", [])
            for err in r.get("errors", [])
        ]
        assert "inline_backtick" in all_error_kinds, (
            f"Expected inline_backtick error in preflight results, got kinds={all_error_kinds!r}"
        )


class TestAC6UnlabeledFenceFail:
    """AC6: unlabeled fence は C4 fail + LP011 fail + preflight static blocked."""

    def test_check_contract_c4_fail(self):
        """C4 must fail for unlabeled fence (no ```bash blocks)."""
        output = _run_check_contract(_UNLABELED_FENCE_BODY)
        c4 = output["deterministic_checks"]["C4_vc_commands_present"]
        assert c4 == "fail", (
            f"C4 should fail for unlabeled fence (no bash fence), got {c4!r}"
        )

    def test_validate_body_lp011_fires(self):
        """LP011 must fire for unlabeled fence (no bash fence)."""
        output = _run_validate_body(_UNLABELED_FENCE_BODY)
        lp011 = [e for e in output["errors"] if e["rule_id"] == "LP011"]
        assert len(lp011) >= 1, "LP011 should fire for unlabeled fence VC"

    def test_preflight_static_blocked(self):
        """Preflight --static-only must return blocked for unlabeled fence."""
        output = _run_preflight_static(_UNLABELED_FENCE_BODY)
        assert output["status"] == "blocked", (
            f"Preflight static should be blocked for unlabeled fence, got {output['status']!r}"
        )


# ---------------------------------------------------------------------------
# AC7: grouped AC marker / inline suffix は 3 checker の合否が一致
# ---------------------------------------------------------------------------

class TestAC7GroupedMarkerParity:
    """AC7: grouped AC marker (# AC2, AC3) は 3 checker すべて pass。

    仕様: grouped AC marker は canonical として扱う (#814 互換)。
    C5, LP010, preflight static で 3 checker の合否が一致すること。
    """

    def test_check_contract_c5_pass(self):
        """C5 must pass for grouped AC markers (# AC2, AC3 recognized)."""
        output = _run_check_contract(_GROUPED_MARKER_BODY)
        c5 = output["deterministic_checks"]["C5_ac_vc_number_alignment"]
        assert c5 == "pass", (
            f"C5 should pass for grouped marker (# AC2, AC3), got {c5!r}"
        )

    def test_validate_body_lp010_no_error(self):
        """LP010 must not fire for grouped AC markers."""
        output = _run_validate_body(_GROUPED_MARKER_BODY)
        lp010 = [e for e in output["errors"] if e["rule_id"] == "LP010"]
        assert len(lp010) == 0, (
            f"LP010 should not fire for grouped AC markers: {lp010}"
        )

    def test_preflight_static_ok(self):
        """Preflight --static-only must return ok for grouped AC markers."""
        output = _run_preflight_static(_GROUPED_MARKER_BODY)
        assert output["status"] == "ok", (
            f"Preflight static should be ok for grouped marker, got {output['status']!r}"
        )


class TestAC7InlineSuffixParity:
    """AC7: inline suffix ($ command # AC1) は 3 checker すべて pass。

    仕様: inline suffix は canonical として扱う（validate_issue_body との互換性維持）。
    C5, LP010, preflight static で 3 checker の合否が一致すること。
    """

    def test_check_contract_c5_pass(self):
        """C5 must pass for inline suffix AC markers."""
        output = _run_check_contract(_INLINE_SUFFIX_BODY)
        c5 = output["deterministic_checks"]["C5_ac_vc_number_alignment"]
        assert c5 == "pass", (
            f"C5 should pass for inline suffix ($ cmd # AC1), got {c5!r}"
        )

    def test_validate_body_lp010_no_error(self):
        """LP010 must not fire for inline suffix AC markers."""
        output = _run_validate_body(_INLINE_SUFFIX_BODY)
        lp010 = [e for e in output["errors"] if e["rule_id"] == "LP010"]
        assert len(lp010) == 0, (
            f"LP010 should not fire for inline suffix: {lp010}"
        )

    def test_preflight_static_ok(self):
        """Preflight --static-only must return ok for inline suffix AC markers."""
        output = _run_preflight_static(_INLINE_SUFFIX_BODY)
        assert output["status"] == "ok", (
            f"Preflight static should be ok for inline suffix, got {output['status']!r}"
        )


# ---------------------------------------------------------------------------
# Unit tests for parse_verification_commands_section (AC1)
# ---------------------------------------------------------------------------

class TestParseVcSectionUnit:
    """AC1: parse_verification_commands_section() unit tests."""

    def setup_method(self):
        import sys
        sys.path.insert(0, str(_CONTRACT_REVIEW_SCRIPTS))
        from vc_contract_syntax import parse_verification_commands_section
        self._parse = parse_verification_commands_section

    def test_canonical_returns_commands(self):
        """Canonical ```bash with # ACN + $ cmd → commands populated."""
        section = textwrap.dedent("""\
            ```bash
            # AC1
            $ uv run pytest tests/ -q
            ```
        """)
        result = self._parse(section)
        assert result.has_bash_fence is True
        assert len(result.commands) == 1
        assert result.commands[0].command == "uv run pytest tests/ -q"
        assert "AC1" in result.commands[0].ac_refs
        assert len(result.errors) == 0

    def test_colon_marker_generates_lp016_error(self):
        """# AC1: text → VcParseError with kind=colon_marker, rule_id=LP016."""
        section = textwrap.dedent("""\
            ```bash
            # AC1: description
            $ uv run pytest tests/ -q
            ```
        """)
        result = self._parse(section)
        assert result.has_bash_fence is True
        lp016_errors = [e for e in result.errors if e.rule_id == "LP016"]
        assert len(lp016_errors) == 1, f"Expected LP016 error, got: {result.errors}"
        assert lp016_errors[0].kind == "colon_marker"
        assert "colon" in lp016_errors[0].fix_hint.lower() or ":" in lp016_errors[0].fix_hint

    def test_unlabeled_fence_generates_error(self):
        """Unlabeled fence → has_unlabeled_fence=True + VcParseError."""
        section = textwrap.dedent("""\
            ```
            # AC1
            $ uv run pytest tests/ -q
            ```
        """)
        result = self._parse(section)
        assert result.has_unlabeled_fence is True
        unlabeled_errors = [e for e in result.errors if e.kind == "unlabeled_fence"]
        assert len(unlabeled_errors) >= 1

    def test_grouped_marker_recognized(self):
        """# AC2, AC3 → both AC2 and AC3 in ac_refs."""
        section = textwrap.dedent("""\
            ```bash
            # AC2, AC3
            $ uv run pytest tests/fixtures/ -q
            ```
        """)
        result = self._parse(section)
        assert "AC2" in result.ac_refs, f"AC2 should be in ac_refs: {result.ac_refs}"
        assert "AC3" in result.ac_refs, f"AC3 should be in ac_refs: {result.ac_refs}"
        assert len(result.errors) == 0

    def test_inline_suffix_recognized(self):
        """$ cmd # AC1 → AC1 in ac_refs, command without suffix."""
        section = textwrap.dedent("""\
            ```bash
            $ uv run pytest tests/ -q # AC1
            ```
        """)
        result = self._parse(section)
        assert "AC1" in result.ac_refs, f"AC1 should be in ac_refs: {result.ac_refs}"
        cmd = result.commands[0].command
        assert "# AC1" not in cmd, f"# AC1 suffix should be stripped from command: {cmd!r}"

    def test_non_dollar_command_generates_error(self):
        """Non-$ command line in bash fence → non_dollar_command error."""
        section = textwrap.dedent("""\
            ```bash
            # AC1
            uv run pytest tests/ -q
            ```
        """)
        result = self._parse(section)
        non_dollar = [e for e in result.errors if e.kind == "non_dollar_command"]
        assert len(non_dollar) >= 1, f"Expected non_dollar_command error: {result.errors}"

    def test_empty_section_returns_empty_result(self):
        """Empty section → empty result."""
        result = self._parse("")
        assert result.has_bash_fence is False
        assert len(result.commands) == 0
        assert len(result.errors) == 0

    def test_baseline_expect_annotation_extracted(self):
        """# baseline-expect: fail annotation is extracted into VcCommandEntry."""
        section = textwrap.dedent("""\
            ```bash
            # AC1
            # baseline-expect: fail
            $ uv run pytest tests/ -q
            ```
        """)
        result = self._parse(section)
        assert len(result.commands) == 1
        assert result.commands[0].baseline_expect == "fail"
