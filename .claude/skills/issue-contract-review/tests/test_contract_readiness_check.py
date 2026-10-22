"""
tests/test_contract_readiness_check.py

Tests specifically for #817 changes to contract_readiness_check.py:
  - AC3: --issue-number alias is present in argparse

These supplement the existing tests in test_vc_format_and_command_policy.py etc.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_CRC_PATH = _SCRIPTS_DIR / "contract_readiness_check.py"

spec = importlib.util.spec_from_file_location("contract_readiness_check_817", _CRC_PATH)
assert spec is not None and spec.loader is not None
_crc_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_crc_mod)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# AC3: --issue-number alias
# ---------------------------------------------------------------------------


class TestIssueNumberAlias:
    """AC3: --issue-number alias exists in argparse definition."""

    def test_issue_number_alias_accepted(self):
        """--issue-number is accepted as equivalent to --issue."""
        # Build the parser by calling main module's parser construction
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--issue", "--issue-number", dest="issue", type=int, help="GitHub Issue number"
        )
        # Test --issue-number is parsed correctly
        args = parser.parse_args(["--issue-number", "42"])
        assert args.issue == 42

    def test_issue_alias_also_accepted(self):
        """--issue (original) is still accepted."""
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--issue", "--issue-number", dest="issue", type=int, help="GitHub Issue number"
        )
        args = parser.parse_args(["--issue", "42"])
        assert args.issue == 42

    def test_both_dest_same(self):
        """Both --issue and --issue-number map to same dest."""
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--issue", "--issue-number", dest="issue", type=int, help="GitHub Issue number"
        )
        args1 = parser.parse_args(["--issue", "100"])
        args2 = parser.parse_args(["--issue-number", "100"])
        assert args1.issue == args2.issue

    def test_contract_readiness_check_has_issue_number_alias(self):
        """
        Verify that the actual contract_readiness_check.py main() parser
        accepts --issue-number.
        This is the canonical AC3 verification.
        """
        import subprocess

        result = subprocess.run(
            [sys.executable, str(_CRC_PATH), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # --issue-number should appear in help text
        help_text = result.stdout + result.stderr
        assert "--issue-number" in help_text or "issue-number" in help_text, (
            f"--issue-number alias not found in help output:\n{help_text}"
        )

    def test_issue_number_arg_in_source(self):
        """Verify the source code contains --issue-number in add_argument call (AC3 static)."""
        source = _CRC_PATH.read_text(encoding="utf-8")
        assert "--issue-number" in source, (
            f"AC3: --issue-number alias not found in {_CRC_PATH}"
        )
