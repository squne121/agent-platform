"""
Regression fixtures for impl-review-loop V2 routing boundary.

Issue #777: Migrated from shadow helper to production consumer import.
Issue #1873: reviewer self-reported merge_ready / required_auto_actions /
allowed_paths_gate / mergeability are no longer routing inputs.
route_loop_verdict_v2() takes reviewer_verdict (verdict, reviewed_head_sha,
blockers, warnings) and live_mergeability (head_sha, mergeable,
merge_state_status) as separate arguments; update_branch actions are
synthesized by the router from live_mergeability, not read from the
reviewer's output. This module freezes the boundary doc wording and the
canonical BEHIND -> update_branch path.

TEST_VERDICT_MACHINE:
  version: 1
  result: pass
  head_sha: "pending"
  commands:
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/test_v2_routing_boundary_regression.py -v"
      exit_code: 0
      stdout_sha256: "pending"
  fixtures:
    - case: "AC3_v2_update_branch_behind"
      after_pass_verified: true
    - case: "AC13_no_new_schema_reviewer_self_report_rejected"
      after_pass_verified: true
  skipped: []
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
BOUNDARY_DOC = REPO_ROOT / "docs" / "dev" / "agent-skill-boundaries.md"
FIXTURES_DIR = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "tests" / "fixtures"
STEP5_FIXTURES_DIR = FIXTURES_DIR / "step5_routing_consumer"

# ---------------------------------------------------------------------------
# Production consumer import (replaces shadow helper)
# ---------------------------------------------------------------------------

SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_step5_fixture(name: str) -> dict:
    return yaml.safe_load((STEP5_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_given_v2_update_branch_fixture_when_routed_then_update_branch_path_is_selected() -> None:
    """AC3: APPROVE + live BEHIND + branch_behind_main confirmed -> route_to_update_branch,
    with the update_branch action synthesized by the router (not read from the reviewer)."""
    fx = _load_step5_fixture("positive_update_branch.yml")
    result = route_loop_verdict_v2(
        fx["reviewer_verdict"],
        fx["live_mergeability"],
    )
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]
    assert result.selected_action is not None
    action = dict(result.selected_action)
    assert action["kind"] == "update_branch"
    assert action["executor"] == "implementation-worker"
    assert action["skill"] == "implement-issue.update_branch"
    assert action["expected_head_sha"] == fx["reviewer_verdict"]["reviewed_head_sha"]


def test_given_reviewer_verdict_with_legacy_self_report_fields_when_routed_then_it_fails_closed() -> None:
    """AC13: no new schema is introduced for reviewer self-report; any legacy
    merge_ready / required_auto_actions / allowed_paths_gate / mergeability
    field on reviewer_verdict must fail closed rather than be silently accepted."""
    for legacy_key, legacy_value in (
        ("merge_ready", True),
        ("required_auto_actions", []),
        ("allowed_paths_gate", {"status": "ok"}),
        ("mergeability", {"mergeable": "MERGEABLE"}),
    ):
        reviewer_verdict = {
            "verdict": "APPROVE",
            "reviewed_head_sha": "abc123def456",
            "blockers": [],
            legacy_key: legacy_value,
        }
        live_mergeability = {
            "head_sha": "abc123def456",
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
        }
        result = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
        assert result.route == "fail_closed", (
            f"[{legacy_key}] Expected fail_closed, got '{result.route}'. "
            f"reason_code: {result.reason_code}"
        )
        assert result.fail_closed is True
