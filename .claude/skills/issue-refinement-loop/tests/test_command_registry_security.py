"""
test_command_registry_security.py

Security tests for command_registry.validate_shell_string() — AC4.

Verifies that all deny-listed shell constructs are blocked,
and that registry-generated argv (when joined) is classified correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as reg  # noqa: E402


# ---------------------------------------------------------------------------
# AC4: deny matrix — all listed patterns must be blocked
# ---------------------------------------------------------------------------

class TestDenyMatrix:
    def _assert_blocked(self, s: str) -> None:
        result = reg.validate_shell_string(s)
        assert result["ok"] is False, (
            f"Expected blocked for {s!r}, but got ok=True"
        )
        assert result["blocked_reason"] is not None

    def test_compound_and(self):
        """&& compound operator is blocked."""
        self._assert_blocked("echo hello && rm -rf /")

    def test_compound_or(self):
        """|| compound operator is blocked."""
        self._assert_blocked("cmd || fallback")

    def test_semicolon(self):
        """Semicolon separator is blocked."""
        self._assert_blocked("a ; b")

    def test_pipe(self):
        """Pipe is blocked."""
        self._assert_blocked("cmd | grep x")

    def test_redirect_out(self):
        """Redirect > is blocked."""
        self._assert_blocked("echo foo > /tmp/bar")

    def test_redirect_in(self):
        """Redirect < is blocked."""
        self._assert_blocked("cat < file")

    def test_append_redirect(self):
        """Append redirect >> is blocked."""
        self._assert_blocked("echo foo >> /tmp/bar")

    def test_heredoc_redirect(self):
        """Heredoc << is blocked."""
        self._assert_blocked("cat << EOF")

    def test_backtick_substitution(self):
        """Backtick command substitution is blocked."""
        self._assert_blocked("`cmd`")

    def test_dollar_paren_substitution(self):
        """$(whoami) command substitution is blocked."""
        self._assert_blocked("$(whoami)")

    def test_process_sub_in(self):
        """<(cat file) process substitution is blocked."""
        self._assert_blocked("<(cat file)")

    def test_process_sub_out(self):
        """>(cat) process substitution is blocked."""
        self._assert_blocked(">(cat)")

    def test_bash_lc(self):
        """bash -lc shell launcher is blocked."""
        self._assert_blocked("bash -lc 'gh issue view 1'")

    def test_sh_c(self):
        """sh -c shell launcher is blocked."""
        self._assert_blocked("sh -c 'echo hi'")

    def test_env_injection(self):
        """env GH_TOKEN=x is blocked."""
        self._assert_blocked("env GH_TOKEN=x gh api ...")

    def test_cd_and_command(self):
        """cd /tmp && gh issue view 1 is blocked."""
        self._assert_blocked("cd /tmp && gh issue view 1")

    def test_cd_alone(self):
        """cd alone is blocked (directory traversal)."""
        self._assert_blocked("cd /tmp")

    def test_bash_alone(self):
        """bare bash is blocked."""
        self._assert_blocked("bash")

    def test_sh_alone(self):
        """bare sh is blocked."""
        self._assert_blocked("sh")

    def test_env_alone(self):
        """bare env is blocked."""
        self._assert_blocked("env")


# ---------------------------------------------------------------------------
# validate_shell_string return shape
# ---------------------------------------------------------------------------

class TestValidateReturnShape:
    def test_ok_result_has_none_reason(self):
        """ok=True result has blocked_reason=None."""
        result = reg.validate_shell_string("uv run pytest tests/ -v")
        assert result["ok"] is True
        assert result["blocked_reason"] is None

    def test_blocked_result_has_reason_string(self):
        """blocked result has blocked_reason as non-empty string."""
        result = reg.validate_shell_string("echo hi && rm -rf /")
        assert result["ok"] is False
        assert isinstance(result["blocked_reason"], str)
        assert result["blocked_reason"]  # non-empty

    def test_result_keys(self):
        """validate_shell_string always returns ok and blocked_reason keys."""
        for s in ["safe command", "unsafe && command"]:
            result = reg.validate_shell_string(s)
            assert "ok" in result
            assert "blocked_reason" in result


# ---------------------------------------------------------------------------
# Registry-generated argv should be intrinsically safe (argv list != shell string)
# Note: validate_shell_string validates strings, not lists.
# We verify that joining argv from registry does NOT introduce shell operators.
# ---------------------------------------------------------------------------

class TestRegistryArgvIsSafe:
    def test_registry_argv_joined_no_deny_tokens(self):
        """Joining registry argv tokens produces no shell operator tokens.

        This verifies the argv is structurally safe when rendered without shell=True.
        Note: validate_shell_string is designed for untrusted shell strings.
        argv lists are NOT passed through validate_shell_string in production
        (they are passed as argv directly to subprocess with shell=False).
        This test confirms the argv content is free of obvious shell operator tokens.
        """
        deny_tokens = {"&&", "||", ";", "|", ">", "<", ">>", "<<", "`", "$("}
        for cmd_id, entry in reg.REGISTRY.items():
            argv = entry["argv"]
            for token in argv:
                # Exclude placeholder tokens {foo} from this check
                if token.startswith("{") and token.endswith("}"):
                    continue
                for deny in deny_tokens:
                    assert deny not in token, (
                        f"Registry {cmd_id!r} argv token {token!r} contains denied operator {deny!r}"
                    )

    def test_render_command_argv_safe(self):
        """render_command output tokens are free of shell operators."""
        deny_tokens = {"&&", "||", ";", "|", ">", "<", "$(", "`"}
        argv = reg.render_command("preflight.run", {"issue_number": 42, "repo": "owner/repo"})
        for token in argv:
            for deny in deny_tokens:
                assert deny not in token, (
                    f"Rendered argv token {token!r} contains denied operator {deny!r}"
                )
