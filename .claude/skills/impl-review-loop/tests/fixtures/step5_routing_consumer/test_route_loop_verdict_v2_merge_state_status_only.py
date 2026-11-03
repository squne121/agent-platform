"""
Destination-state tests for Issue #1856 (evidence authority cutover, Phase 1)
/ Issue #1870 (#1856 AC1): route_loop_verdict_v2() does not accept a
test_verdict argument of any kind.

These tests assert that route_loop_verdict_v2():

- rejects a test_verdict keyword argument outright (TypeError) -- it is not
  part of the public API, not even as an optional/ignored parameter.
- AC2: derives BEHIND-routing consistency solely from
  `live_mergeability["merge_state_status"]`, without any branch_behind_main /
  test_verdict cross-check gating the outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# parents[3] = .claude/skills/impl-review-loop (from fixtures/step5_routing_consumer/test_*.py)
IMPL_REVIEW_LOOP_DIR = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = IMPL_REVIEW_LOOP_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402

FIXTURE_DIR = Path(__file__).parent


def _load_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_test_verdict_argument_removed():
    """Issue #1870 (#1856) AC1: route_loop_verdict_v2() no longer accepts a
    test_verdict argument -- passing one raises TypeError."""
    reviewer_verdict = {
        "verdict": "APPROVE",
        "reviewed_head_sha": "abc123def456",
        "blockers": [],
    }
    live_mergeability = {
        "head_sha": "abc123def456",
        "mergeable": "MERGEABLE",
        "merge_state_status": "BEHIND",
    }
    with pytest.raises(TypeError):
        route_loop_verdict_v2(  # type: ignore[call-arg]
            reviewer_verdict, live_mergeability, test_verdict={"branch_behind_main": False},
        )


def test_behind_action_consistency_via_merge_state_status_only():
    """AC2: BEHIND routing consistency, derived solely from
    live_mergeability.merge_state_status -- no test_verdict input exists."""
    fx = _load_fixture("positive_behind.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "route_to_update_branch", (
        f"Expected route_to_update_branch, got route={result.route!r} errors={result.errors}"
    )
    assert result.fail_closed is False
