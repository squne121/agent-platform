"""
conftest.py for .claude/skills/issue-contract-review/scripts/tests/

Issue #2207 OWNER P1-2 item 6 (PR #2221 REQUEST_CHANGES): a bare
`pytest.mark.skipif` is indistinguishable from "test not applicable" --
pytest reports a skipped-only run with exit code 0, the SAME code as a
genuine PASS. AC9/AC10's own `Runtime Verification Applicability` contract
(Issue #2207 skip_conditions) requires that a POSIX-unsupported environment
be reported as "environment blocked" via a DISTINCT exit code (77, the
repo-wide convention for "SKIP due to environment", see e.g.
`.claude/skills/gemini-cli-headless-delegation/tests/test_agy_*`), not a
silent exit-0 skip.

This hook only overrides the session exit code when EVERY item that ran in
THIS session was skipped for an "environment blocked" reason -- it never
fires when the module runs as part of a larger suite alongside passing
tests (in that case pytest's own exit code is already a meaningful 0), so
it cannot mask a real regression in unrelated tests.
"""

from __future__ import annotations

import pytest

_ENVIRONMENT_BLOCKED_MARKER = "environment blocked"
_ENVIRONMENT_BLOCKED_EXIT_CODE = 77


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus != 0:
        return
    if not session.items:
        return

    terminal_reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if terminal_reporter is None:
        return

    skipped_reports = terminal_reporter.stats.get("skipped", [])
    if not skipped_reports:
        return
    if len(skipped_reports) != len(session.items):
        # Mixed pass/skip (or fail/skip) in this session -- leave pytest's
        # own exit code alone.
        return

    for report in skipped_reports:
        reason = ""
        longrepr = getattr(report, "longrepr", None)
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            reason = str(longrepr[2])
        if _ENVIRONMENT_BLOCKED_MARKER not in reason:
            # At least one skip was for an ordinary (non-environment)
            # reason -- do not claim "environment blocked" for the whole
            # session.
            return

    session.exitstatus = _ENVIRONMENT_BLOCKED_EXIT_CODE
