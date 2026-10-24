"""Issue #2610: `root_review_pipeline.produce` outer timeout ownership.

Fresh 5-item inventory (execution surface / registry timeout consumer /
outer deadline owner / signal producer / process cleanup owner) performed
for this Issue found NO repository-owned consumer that actually reads and
enforces `command_registry.py`'s `root_review_pipeline.produce.timeout_seconds`
(90) as a real subprocess deadline: that command_id is not one of
`scripts/agent-guards/skill_runtime_command_policy.py`'s
`eligible_command_ids`, and `skill_runtime_exec.py` -- the only in-repo
reader of any REGISTRY entry's `timeout_seconds` -- therefore never
dispatches it (`test_ac1_*` below pins this down as a regression guard).

Per the Issue's own Runtime Verification Applicability `skip_conditions`,
this "no repository-owned consumer found" result means the AC2/AC3 "named
SSOT for a real in-repo consumer's outer deadline" production change is
OUT OF SCOPE for this file -- inventing a fictitious consumer, outer
watchdog, or persistent state just to exercise that code path would itself
violate this Issue's Stop Conditions (no new watchdog script / schema /
ledger / receipt / persistent state). This file instead covers what
remains fully in-scope regardless of consumer inventory result: AC1 (the
inventory finding itself, pinned as a regression guard), and AC4/AC5 (the
EXISTING, real timeout/cancellation/fail-closed semantics that already live
in `reviewer_transport.run_reviewer_transport()` -- the actual
SIGTERM->bounded grace->SIGKILL->reap implementation -- and in
`run_root_review_pipeline._cmd_produce()`'s fail-closed classification of a
transport failure). Both are exercised here via test-only production-shaped
fixtures (a real subprocess with a tiny injected deadline; no 90-second or
520-second real-time wait), per In Scope (g).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest  # noqa: F401 (used for capsys/monkeypatch fixtures via test signatures)

REFINEMENT_SCRIPTS = Path(__file__).parent.parent / "scripts"
if str(REFINEMENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(REFINEMENT_SCRIPTS))
import command_registry  # noqa: E402
import reviewer_transport as transport  # noqa: E402
import run_root_review_pipeline as pipeline  # noqa: E402

GUARDS_SCRIPTS = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards"
if str(GUARDS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(GUARDS_SCRIPTS))
import skill_runtime_command_policy as policy  # noqa: E402
from _trusted_repo import TRUSTED_REPO  # noqa: E402

_PRODUCE_COMMAND_ID = "root_review_pipeline.produce"

# A minimal body whose canonical plan is N<=2 (compatibility deadlines).
_LOW_N_BODY = """## Verification Commands

```bash
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_a.py -q
```
"""


# ---------------------------------------------------------------------------
# AC1: fresh inventory regression guard -- no repository-owned consumer
# reads/enforces this command_id's registry `timeout_seconds` today.
# ---------------------------------------------------------------------------


def test_ac1_produce_command_id_is_not_a_skill_runtime_eligible_command() -> None:
    """Item (2) of the 5-item inventory (registry timeout consumer): pins
    down that `root_review_pipeline.produce` is NOT in
    `skill_runtime_command_policy.py`'s `eligible_command_ids` -- i.e.
    `skill_runtime_exec.py` never dispatches it and therefore never applies
    its declared `timeout_seconds` as a real subprocess deadline. If this
    ever flips to True, the "no repository-owned consumer" premise this
    file (and the Issue #2610 AC2/AC3 skip) relies on no longer holds and
    this test must be revisited alongside a real named-SSOT implementation."""
    eligible = policy.SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]
    assert _PRODUCE_COMMAND_ID not in eligible


def test_ac1_registry_declares_the_command_with_a_static_ninety_second_value() -> None:
    entry = command_registry.REGISTRY[_PRODUCE_COMMAND_ID]
    assert entry["timeout_seconds"] == 90
    assert entry["mutation"] is False


def test_ac1_declared_registry_value_is_not_wired_into_the_real_inner_budget() -> None:
    """Non-enforcement / drift regression (Issue #2610 Timeout Ownership
    Decision, "fact (high confidence)"): the registry's static 90 is NOT
    the value `reviewer_transport.py` actually uses as its fallback
    per-attempt/total deadline, and it is well below both the
    minimum-compatibility ReviewBudget (per-attempt 480s / total 520s) this
    same module declares. This demonstrates the 90 is informational, not a
    real enforced budget anywhere in the current architecture."""
    registry_timeout = command_registry.REGISTRY[_PRODUCE_COMMAND_ID]["timeout_seconds"]
    assert registry_timeout != transport.PER_ATTEMPT_DEADLINE_SECONDS
    assert registry_timeout != transport.TOTAL_DEADLINE_SECONDS
    assert registry_timeout < transport.PER_ATTEMPT_DEADLINE_SECONDS
    assert registry_timeout < transport.TOTAL_DEADLINE_SECONDS


# ---------------------------------------------------------------------------
# AC4: real SIGTERM -> bounded grace -> SIGKILL -> reap semantics, exercised
# against a real child process via a test-only tiny deadline injection (no
# real-time 90s/520s wait). This is the ACTUAL enforcement mechanism in the
# current architecture (the invocation-local inner `ReviewBudget`), which
# In Scope (f) requires to be preserved, not weakened, by this Issue.
# ---------------------------------------------------------------------------


def test_ac4_timeout_path_sigterms_then_sigkills_an_unresponsive_child_and_reaps_it(tmp_path: Path) -> None:
    """A real child that ignores SIGTERM (forcing the SIGKILL branch) is
    bounded-terminated well within a couple of seconds, never the
    90-second/520-second real budgets, and is confirmed reaped."""
    ignore_sigterm_script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )
    base_argv = [sys.executable, "-c", ignore_sigterm_script]

    started = time.monotonic()
    result = transport.run_reviewer_transport(
        base_argv=base_argv,
        command_id="root_review_pipeline.checker_attempt",
        argv_template_id="root_review_pipeline.checker_attempt/v1",
        backend="deterministic",
        issue_number=999902,
        repo=TRUSTED_REPO,
        reviewed_body_sha256=pipeline.sha256_of(_LOW_N_BODY),
        artifact_root=tmp_path,
        per_attempt_deadline=1,
        total_deadline=1,
    )
    elapsed = time.monotonic() - started

    # Bounded by the process-group reap flow (~1s wait + <=3s TERM grace +
    # <=3s KILL wait + bounded reader join), never anywhere near the old
    # static 90s registry value or the real 480s/520s ReviewBudget minimums.
    assert elapsed < 15, f"timeout handling took {elapsed}s -- expected a bounded few seconds"

    assert result["transport_status"] == "environment_failure"
    assert len(result["attempts"]) == 1, "backend=deterministic must not retry a 'timeout' reason_code"
    attempt = result["attempts"][0]
    assert attempt["timeout"] is True
    assert attempt["reason_code"] == "timeout"
    assert attempt["descendants_reaped"] is True
    # The child ignored SIGTERM, so it can only have exited via SIGKILL (-9).
    assert attempt["signal"] == 9


def test_ac4_timeout_path_handles_a_child_that_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    """A well-behaved child (default SIGTERM disposition) is terminated in
    the FIRST phase of the grace ladder and never needs SIGKILL."""
    sleep_script = "import time\ntime.sleep(30)\n"
    base_argv = [sys.executable, "-c", sleep_script]

    result = transport.run_reviewer_transport(
        base_argv=base_argv,
        command_id="root_review_pipeline.checker_attempt",
        argv_template_id="root_review_pipeline.checker_attempt/v1",
        backend="deterministic",
        issue_number=999903,
        repo=TRUSTED_REPO,
        reviewed_body_sha256=pipeline.sha256_of(_LOW_N_BODY),
        artifact_root=tmp_path,
        per_attempt_deadline=1,
        total_deadline=1,
    )
    attempt = result["attempts"][0]
    assert attempt["timeout"] is True
    assert attempt["reason_code"] == "timeout"
    assert attempt["descendants_reaped"] is True
    # Default SIGTERM disposition terminates the process -> signal 15.
    assert attempt["signal"] == 15


# ---------------------------------------------------------------------------
# AC5: fail-closed classification -- a transport failure (including one
# caused by AC4's timeout path) must never be routed as an approve / any
# non-fail-closed canonical Step 2 route.
# ---------------------------------------------------------------------------


def _make_produce_args(issue_number: int = 999904, repo: str = TRUSTED_REPO):
    import argparse

    return argparse.Namespace(issue_number=issue_number, repo=repo)


def test_ac5_produce_does_not_fabricate_a_route_on_reviewer_transport_timeout(monkeypatch, capsys) -> None:
    """When `run_reviewer_transport()` reports a non-'ok' `transport_status`
    (the exact shape a real, unretried timeout produces -- Issue #2054's
    established environment_failure/fail-closed classification, which
    Issue #2610 explicitly preserves), `_cmd_produce()` must classify the
    result as `canonical_step2_route:
    fail_closed_environment_or_integrity_failure` and never fabricate
    `canonical_step2_route`, `approve`, or implementation authorization."""

    def fake_fetch_and_pin_live_body(issue_number, repo, **kwargs):
        return _LOW_N_BODY, pipeline.sha256_of(_LOW_N_BODY), None

    def fake_run_reviewer_transport(**kwargs):
        # Shape returned by a real, non-retried timeout (backend="deterministic").
        return {
            "schema": "REVIEWER_TRANSPORT_RESULT_V1",
            "transport_status": "environment_failure",
            "semantic_verdict": None,
            "invocation_id": "fixture-invocation-2610-timeout",
            "attempts": [
                {
                    "schema": "REVIEWER_TRANSPORT_ATTEMPT_RESULT_V1",
                    "transport_status": "environment_failure",
                    "semantic_verdict": None,
                    "timeout_phase": "reviewer_transport_wait",
                    "attempt": 1,
                    "timeout": True,
                    "reason_code": "timeout",
                    "descendants_reaped": True,
                    "signal": 9,
                }
            ],
        }

    monkeypatch.setattr(pipeline, "fetch_and_pin_live_body", fake_fetch_and_pin_live_body)
    monkeypatch.setattr(pipeline._reviewer_transport, "run_reviewer_transport", fake_run_reviewer_transport)

    exit_code = pipeline._cmd_produce(_make_produce_args())
    assert exit_code == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["status"] == "input_or_runtime_error"
    assert payload["error_code"] == "reviewer_transport_environment_failure"
    assert payload["canonical_step2_route"] == "fail_closed_environment_or_integrity_failure"

    # No fabricated authorization / approval anywhere in the emitted payload.
    assert payload["canonical_step2_route"] != "step_2_5"
    assert "approve" not in json.dumps(payload).lower()
    assert "compact_result" not in payload
    assert "merged_review_result" not in payload
