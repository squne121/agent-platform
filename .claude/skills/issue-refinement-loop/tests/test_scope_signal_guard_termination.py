"""
test_scope_signal_guard_termination.py

Regression tests for Issue #919:
scope_signal_guard 停止時の termination_cause 正規化を検証する。

#1873 (bounded review loops): the renderer/publisher-facing assertions that
used to live here (`render_termination_report.py` / `rtr.render()` /
`rtr._validate_input()`) were removed along with `render_termination_report.
py` itself -- the orchestrator now assembles a plain markdown termination
summary directly (see `publish_termination_report.py`) instead of routing
through a TERMINATION_REPORT_INPUT_V1 renderer. What remains here is the
part of the #919 contract still owned by `decide_next_loop_action.py`: the
scope_signal_guard hard-stop must still emit
`TERMINATION_CAUSE: human_judgment_required` and preserve the reason_code in
BLOCKERS.

AC coverage:
  AC1: scope_signal_guard.triggered=true, reason_code=new_allowed_path_layer
       -> decide_next_loop_action.py の TERMINATION_CAUSE が human_judgment_required になる
  AC2: scope_signal_guard.reason_code は BLOCKERS に残る
  AC4: max_iterations_exceeded / approve / needs-fix の既存挙動が変わらない
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

# Fixture path: uses the same fixture as test_decide_next_loop_action.py
_FIXTURE_PATH = SKILL_ROOT / "fixtures" / "loop_state_v1_fixture.json"


# ---------------------------------------------------------------------------
# Helpers: decide_next_loop_action subprocess runner
# ---------------------------------------------------------------------------

def _load_base_fixture() -> dict:
    """Load the canonical loop_state_v1 fixture (same as other decide_next_loop_action tests)."""
    assert _FIXTURE_PATH.exists(), f"Missing fixture: {_FIXTURE_PATH}"
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _run_decide(state: dict, verdict: str = "needs-fix") -> subprocess.CompletedProcess:
    """Run decide_next_loop_action.py as subprocess and return the result."""
    state_json = json.dumps(state)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "decide_next_loop_action.py"),
         "--loop-state-json", state_json,
         "--review-result-verdict", verdict],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC1: scope_signal_guard.triggered=true, reason_code=new_allowed_path_layer
#       -> decide_next_loop_action emits TERMINATION_CAUSE: human_judgment_required
# ---------------------------------------------------------------------------

class TestAC1ScopeSignalGuardTerminationCauseNormalization:

    def test_decide_emits_termination_cause_human_judgment_required(self):
        """AC1: scope_signal_guard.triggered=true → TERMINATION_CAUSE: human_judgment_required"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_allowed_path_layer",
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2, f"Expected exit 2 (human_escalation), got {result.returncode}"
        assert "TERMINATION_CAUSE: human_judgment_required" in result.stdout, (
            f"Expected TERMINATION_CAUSE: human_judgment_required in stdout.\nActual stdout: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# AC2: scope_signal_guard.reason_code は BLOCKERS に残る
# ---------------------------------------------------------------------------

class TestAC2ReasonCodePreservedInBlockers:

    def test_reason_code_new_allowed_path_layer_in_blockers(self):
        """AC2: reason_code=new_allowed_path_layer は BLOCKERS に残る"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_allowed_path_layer",
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2
        assert "scope_signal_guard_triggered" in result.stdout
        assert "new_allowed_path_layer" in result.stdout, (
            f"Expected reason_code new_allowed_path_layer in BLOCKERS.\nActual stdout: {result.stdout!r}"
        )

    def test_reason_code_without_scope_signal_guard_trigger_absent(self):
        """AC2: scope_signal_guard.triggered=false → reason_code は BLOCKERS に出ない"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_allowed_path_layer",
        }
        state["iteration"] = 0
        state["max_iterations"] = 3
        result = _run_decide(state, verdict="approve")
        assert "new_allowed_path_layer" not in result.stdout

    def test_no_reason_code_still_has_scope_signal_guard_triggered(self):
        """AC2: reason_code なしでも scope_signal_guard_triggered は BLOCKERS に出る"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2
        assert "scope_signal_guard_triggered" in result.stdout


# ---------------------------------------------------------------------------
# AC4: 既存挙動が変わらない
# ---------------------------------------------------------------------------

class TestAC4ExistingBehaviorUnchanged:

    def test_decide_max_iterations_exceeded_still_works(self):
        """AC4: max_iterations 超過の既存挙動が変わらない"""
        state = _load_base_fixture()
        state["iteration"] = 2
        state["max_iterations"] = 3
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2
        assert "max_iterations_exceeded" in result.stdout
        # TERMINATION_CAUSE should be max_iterations_exceeded for this case
        assert "TERMINATION_CAUSE: max_iterations_exceeded" in result.stdout

    def test_decide_approve_still_works(self):
        """AC4: approve verdict の既存挙動が変わらない"""
        state = _load_base_fixture()
        state["iteration"] = 0
        state["max_iterations"] = 3
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="approve")
        assert result.returncode == 0
        assert "proceed_to_step_4_5" in result.stdout

    def test_decide_needs_fix_within_limit_still_works(self):
        """AC4: needs-fix かつ iteration 上限前の既存挙動が変わらない"""
        state = _load_base_fixture()
        state["iteration"] = 0
        state["max_iterations"] = 3
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 0
        assert "continue_to_step_4" in result.stdout
