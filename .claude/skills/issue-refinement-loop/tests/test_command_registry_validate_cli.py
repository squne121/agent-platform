"""Issue #2610 AC8: thin pytest wrapper around `command_registry.py --validate`.

Replaces the previously-raw `uv run python3
.claude/skills/issue-refinement-loop/scripts/command_registry.py --validate`
invocation in the `## Verification Commands` section with a pytest-native
subprocess wrapper, so this VC entry is a `pytest ...` invocation that the
preflight allowlist accepts, instead of a raw direct CLI call (Issue #2610
In Scope (e), VC preflight allowlist constraint).

This wrapper is intentionally thin: it does not reimplement
`validate_registry()`'s own structural-integrity checks (side-effect-free
static checks such as placeholder/type consistency across the whole
REGISTRY -- see `command_registry.py`'s own module for those). It only
asserts the CLI's documented exit-code / stdout contract:

- exit 0 with a stdout `PASS:` summary when the registry is internally
  consistent
- (not exercised here) exit 1 with per-command diagnostics on stderr
  otherwise

This does not gate on the full `scripts/agent-guards` suite or any live
GitHub mutation -- it is scoped to this one CLI's own `--validate` contract.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "command_registry.py"


def _run_validate() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--validate"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_ac8_script_file_exists() -> None:
    assert _SCRIPT.is_file()


def test_ac8_validate_cli_exits_zero() -> None:
    result = _run_validate()
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_ac8_validate_cli_prints_pass_summary_with_no_structural_inconsistencies() -> None:
    result = _run_validate()
    assert result.stdout.startswith("PASS:"), result.stdout
    assert "no structural inconsistencies found" in result.stdout
    assert result.stderr == ""


def test_ac8_validate_cli_summary_reports_a_positive_registry_entry_count() -> None:
    result = _run_validate()
    # "PASS: <N> registry entries validated, no structural inconsistencies found"
    prefix = "PASS: "
    assert result.stdout.startswith(prefix)
    count_token = result.stdout[len(prefix) :].split(" ", 1)[0]
    assert count_token.isdigit()
    assert int(count_token) > 0
