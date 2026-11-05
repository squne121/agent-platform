"""#1851 AC5: contract_snapshot advisory routing must not hard-block Step 1.

`missing_go` / `stale` / `runtime_error` are advisory-only normalized
statuses -- `_next_action_route` must return `proceed_to_step_1` for all of
them (as long as `issue_ready_status == "pass"`). `latest_blocked` (a
trusted-author human veto) is intentionally out of scope and must keep
routing to `run_contract_blocker_triage`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


TEST_REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    TEST_REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "build_intake_capsule.py"
)

spec = importlib.util.spec_from_file_location("build_intake_capsule_advisory_routing", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def test_missing_go_routes_to_proceed_to_step_1():
    route = mod._next_action_route("pass", {"normalized_status": "missing_go"})
    assert route == "proceed_to_step_1"


def test_stale_routes_to_proceed_to_step_1():
    route = mod._next_action_route("pass", {"normalized_status": "stale"})
    assert route == "proceed_to_step_1"


def test_runtime_error_routes_to_proceed_to_step_1():
    route = mod._next_action_route("pass", {"normalized_status": "runtime_error"})
    assert route == "proceed_to_step_1"


def test_go_still_routes_to_proceed_to_step_1():
    route = mod._next_action_route("pass", {"normalized_status": "go"})
    assert route == "proceed_to_step_1"


def test_latest_blocked_human_veto_boundary_unchanged():
    route = mod._next_action_route("pass", {"normalized_status": "latest_blocked"})
    assert route == "run_contract_blocker_triage"


def test_issue_not_ready_still_requests_readiness_check():
    route = mod._next_action_route("fail", {"normalized_status": "missing_go"})
    assert route == "request_readiness_check"


def test_unknown_normalized_status_still_human_review_required():
    route = mod._next_action_route("pass", {"normalized_status": "something_unrecognized"})
    assert route == "human_review_required"
