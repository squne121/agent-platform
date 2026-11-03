"""
Fixture-driven unit tests for route_loop_verdict_v2 production consumer.

Issue #1873: route_loop_verdict_v2 no longer accepts reviewer self-reported
merge_ready / required_auto_actions / allowed_paths_gate / mergeability.
Instead it takes reviewer_verdict (verdict, reviewed_head_sha, blockers,
warnings) and live_mergeability (head_sha, mergeable, merge_state_status)
as its only two arguments. Issue #1870 (#1856): there is no test_verdict
parameter at all; BEHIND routing is derived solely from
live_mergeability.merge_state_status.

Each fixture file defines:
  - reviewer_verdict: minimal result convention dict
  - live_mergeability: live GitHub PR state dict
  - expected.route: expected RouteDecision.route value
  - expected.fail_closed: expected RouteDecision.fail_closed value
  - expected.reason_code: optional exact match for RouteDecision.reason_code
  - expected.reason_code_prefix: optional prefix match
  - expected.selected_action: optional exact dict match for RouteDecision.selected_action

Named tests below (beyond the generic fixture-driven `test_fixture`) exist to
give the mandatory #1860/#1871/#1873 regression names discoverability and an
explicit failure message independent of fixture-file bookkeeping.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest
import yaml

IMPL_REVIEW_LOOP_DIR = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = IMPL_REVIEW_LOOP_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import ROUTE_ALREADY_SATISFIED, RouteDecision, route_loop_verdict_v2  # noqa: E402

FIXTURE_DIR = Path(__file__).parent


def _fixture_files() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.yml"))


def _load_fixture(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_fixture_by_name(name: str) -> dict:
    return _load_fixture(FIXTURE_DIR / name)


@pytest.mark.parametrize("fixture_path", _fixture_files(), ids=lambda p: p.stem)
def test_fixture(fixture_path: Path):
    fx = _load_fixture(fixture_path)
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])

    expected = fx["expected"]

    assert result.route == expected["route"], (
        f"{fixture_path.name}: expected route '{expected['route']}', "
        f"got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is expected["fail_closed"], (
        f"{fixture_path.name}: expected fail_closed={expected['fail_closed']}, "
        f"got {result.fail_closed}"
    )

    if "reason_code" in expected:
        assert result.reason_code == expected["reason_code"], (
            f"{fixture_path.name}: expected reason_code={expected['reason_code']!r}, "
            f"got {result.reason_code!r}"
        )

    if "reason_code_prefix" in expected:
        prefix = expected["reason_code_prefix"]
        assert result.reason_code is not None and result.reason_code.startswith(prefix), (
            f"{fixture_path.name}: expected reason_code to start with {prefix!r}, "
            f"got {result.reason_code!r}"
        )

    if "selected_action" in expected:
        assert result.selected_action is not None
        assert dict(result.selected_action) == expected["selected_action"], (
            f"{fixture_path.name}: selected_action mismatch: "
            f"{dict(result.selected_action)} != {expected['selected_action']}"
        )


def test_at_least_one_positive_and_one_negative_fixture_present():
    names = [p.stem for p in _fixture_files()]
    assert any(n.startswith("positive_") for n in names)
    assert any(n.startswith("negative_") for n in names)


# ---------------------------------------------------------------------------
# Issue #1870 (#1856) / #1873: no test_verdict parameter anywhere.
# ---------------------------------------------------------------------------


def test_router_signature_has_no_test_verdict():
    """route_loop_verdict_v2 must take exactly reviewer_verdict and
    live_mergeability -- no test_verdict, no third parameter of any kind."""
    sig = inspect.signature(route_loop_verdict_v2)
    param_names = list(sig.parameters.keys())
    assert param_names == ["reviewer_verdict", "live_mergeability"], (
        f"Unexpected router signature: {param_names!r}"
    )
    assert "test_verdict" not in param_names


def test_legacy_reviewer_authority_fields_rejected():
    """A reviewer_verdict carrying any V1/V2-legacy or test_verdict field
    must fail closed rather than being silently accepted as routing input."""
    for legacy_key in ("merge_ready", "required_auto_actions", "allowed_paths_gate",
                       "mergeability", "mergeStateStatus", "recommendations",
                       "test_verdict"):
        reviewer_verdict = {
            "verdict": "APPROVE",
            "reviewed_head_sha": "abc123",
            "blockers": [],
            legacy_key: "anything",
        }
        live_mergeability = {
            "head_sha": "abc123",
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
        }
        result = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
        assert result.fail_closed is True, f"legacy field {legacy_key} was not rejected"
        assert result.reason_code == f"schema_invalid_legacy_field_present:{legacy_key}"


# Backward-compatible alias name (kept discoverable under its original name).
test_no_legacy_v1_fields_accepted_at_all = test_legacy_reviewer_authority_fields_rejected


# ---------------------------------------------------------------------------
# #1860 Owner Decision / PR #1871 (#1869): actual conflict precedes verdict.
# ---------------------------------------------------------------------------


def test_conflict_precedes_request_changes():
    """A real conflict must hard-stop even when the reviewer verdict is
    REQUEST_CHANGES -- the conflict check runs before verdict dispatch."""
    reviewer_verdict = {
        "verdict": "REQUEST_CHANGES",
        "reviewed_head_sha": "abc123",
        "blockers": ["needs more work"],
    }
    live_mergeability = {
        "head_sha": "abc123",
        "mergeable": "CONFLICTING",
        "merge_state_status": "DIRTY",
    }
    result = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
    assert result.route == "conflict_hard_stop", (
        f"expected conflict_hard_stop, got {result.route!r} (errors: {result.errors})"
    )


def test_conflict_precedes_human_review_required():
    """A real conflict must hard-stop even when the reviewer verdict is
    HUMAN_REVIEW_REQUIRED -- the conflict check runs before verdict
    dispatch."""
    reviewer_verdict = {
        "verdict": "HUMAN_REVIEW_REQUIRED",
        "reviewed_head_sha": "abc123",
        "blockers": ["ambiguous scope"],
    }
    live_mergeability = {
        "head_sha": "abc123",
        "mergeable": "MERGEABLE",
        "merge_state_status": "DIRTY",
    }
    result = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
    assert result.route == "conflict_hard_stop", (
        f"expected conflict_hard_stop, got {result.route!r} (errors: {result.errors})"
    )


def test_mergeable_conflicting_is_hard_stop():
    fx = _load_fixture_by_name("negative_conflicting_mergeable.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "conflict_hard_stop"
    assert result.fail_closed is False
    assert result.reason_code is not None
    assert result.reason_code.startswith("conflict_mergeable_CONFLICTING")


def test_dirty_is_hard_stop():
    fx = _load_fixture_by_name("negative_dirty_merge_state.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "conflict_hard_stop"
    assert result.fail_closed is False
    assert result.reason_code is not None
    assert result.reason_code.startswith("conflict_merge_state_status_DIRTY")


def test_merge_state_status_conflicting_is_schema_invalid():
    """merge_state_status=CONFLICTING is not a valid GitHub MergeStateStatus
    enum member -> schema_invalid, NOT treated as a conflict signal."""
    fx = _load_fixture_by_name("negative_merge_state_status_conflicting_schema_invalid.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "fail_closed"
    assert result.route != "conflict_hard_stop"
    assert result.fail_closed is True
    assert result.reason_code is not None
    assert result.reason_code.startswith("schema_invalid_merge_state_status_value")


# ---------------------------------------------------------------------------
# Stale head / BEHIND synthesis.
# ---------------------------------------------------------------------------


def test_stale_approve_routes_to_rereview():
    fx = _load_fixture_by_name("negative_stale_head.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "route_stale_head_rereview"
    assert result.fail_closed is False


def test_behind_synthesizes_update_branch():
    fx = _load_fixture_by_name("positive_behind.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "route_to_update_branch"
    assert result.fail_closed is False
    assert result.selected_action is not None
    assert result.rerun_required == {"verification": True, "pr_review": True}


def test_update_branch_expected_head_sha_matches_reviewed_head():
    fx = _load_fixture_by_name("positive_update_branch.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.selected_action is not None
    assert result.selected_action["expected_head_sha"] == fx["reviewer_verdict"]["reviewed_head_sha"]


# ---------------------------------------------------------------------------
# UNKNOWN / BLOCKED / UNSTABLE / DRAFT / HAS_HOOKS: none are conflicts, and
# none are automatic human escalations (#1860 Owner Decision / PR #1871 P0-3).
# ---------------------------------------------------------------------------


def test_unknown_is_not_conflict():
    for fixture_name in ("negative_unknown_mergeable.yml", "negative_unknown_merge_state_status.yml"):
        fx = _load_fixture_by_name(fixture_name)
        result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
        assert result.route not in ("conflict_hard_stop", "route_human_escalation"), (
            f"{fixture_name}: UNKNOWN must not be treated as a conflict or "
            f"human escalation, got route={result.route!r}"
        )
        assert result.route == "fail_closed"
        assert result.reason_code == "mergeability_unknown"


def test_blocked_is_not_conflict():
    fx = _load_fixture_by_name("positive_blocked_defer_to_ci.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route not in ("conflict_hard_stop", "route_human_escalation")
    assert result.route == "fail_closed"
    assert result.reason_code is not None
    assert result.reason_code.startswith("merge_state_status_blocked_not_conflict")


def test_unstable_is_not_conflict():
    fx = _load_fixture_by_name("positive_unstable_defer_to_ci.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route not in ("conflict_hard_stop", "route_human_escalation")
    assert result.route == "fail_closed"
    assert result.reason_code is not None
    assert result.reason_code.startswith("merge_state_status_unstable_not_conflict")


def test_draft_is_not_conflict_or_automatic_human_stop():
    fx = _load_fixture_by_name("positive_draft_defer_to_ci.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route not in ("conflict_hard_stop", "route_human_escalation"), (
        "DRAFT must never trigger automatic human escalation on its own "
        "(Issue #1873 Delivery Rule requires the PR to stay Draft pending "
        "human final merge)"
    )
    assert result.route == "fail_closed"
    assert result.reason_code is not None
    assert result.reason_code.startswith("merge_state_status_draft_not_conflict")


def test_has_hooks_is_not_conflict():
    fx = _load_fixture_by_name("positive_has_hooks_approved.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route != "conflict_hard_stop"
    assert result.route == "approved"
    assert result.fail_closed is False


# ---------------------------------------------------------------------------
# APPROVE-with-blockers is an inconsistent reviewer result.
# ---------------------------------------------------------------------------


def test_approve_with_blockers_fails_closed():
    fx = _load_fixture_by_name("negative_approve_with_blockers.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "fail_closed"
    assert result.fail_closed is True
    assert result.reason_code == "approve_with_blockers_inconsistent"


# ---------------------------------------------------------------------------
# Extra 6: RouteDecision.selected_action and rerun_required are immutable
# ---------------------------------------------------------------------------


def test_route_decision_selected_action_is_immutable():
    """Extra 6: selected_action must be MappingProxyType (immutable)."""
    from types import MappingProxyType
    fx = _load_fixture_by_name("positive_update_branch.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.selected_action is not None
    assert isinstance(result.selected_action, MappingProxyType), (
        f"Expected MappingProxyType, got {type(result.selected_action)}"
    )
    with pytest.raises(TypeError):
        result.selected_action["kind"] = "hacked"  # type: ignore[index]


def test_route_decision_rerun_required_is_immutable():
    """Extra 6: rerun_required must be MappingProxyType (immutable)."""
    from types import MappingProxyType
    fx = _load_fixture_by_name("positive_update_branch.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert isinstance(result.rerun_required, MappingProxyType), (
        f"Expected MappingProxyType, got {type(result.rerun_required)}"
    )
    with pytest.raises(TypeError):
        result.rerun_required["verification"] = False  # type: ignore[index]


# ---------------------------------------------------------------------------
# Pure unit tests for AC1 (no subprocess / import side effects)
# ---------------------------------------------------------------------------


def test_module_no_forbidden_imports():
    """Extra 7 (AST-based): route_loop_verdict_v2.py must not import forbidden modules."""
    src = SCRIPTS_DIR / "route_loop_verdict_v2.py"
    tree = ast.parse(src.read_text())
    forbidden = {"subprocess", "socket", "urllib", "requests", "httpx", "os"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                names = [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                assert root not in forbidden, (
                    f"Forbidden import in route_loop_verdict_v2.py: {name!r}"
                )


def test_route_decision_is_frozen():
    """AC2: RouteDecision must be frozen (immutable)."""
    rd = RouteDecision(
        route="approved",
        fail_closed=False,
        reason_code=None,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
        errors=(),
    )
    with pytest.raises((AttributeError, TypeError)):
        rd.route = "continue_loop"  # type: ignore[misc]


def test_route_decision_fields():
    """AC2: RouteDecision must have all required fields."""
    rd = RouteDecision(
        route="approved",
        fail_closed=False,
        reason_code=None,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
        errors=(),
    )
    assert hasattr(rd, "route")
    assert hasattr(rd, "fail_closed")
    assert hasattr(rd, "reason_code")
    assert hasattr(rd, "rerun_required")
    assert hasattr(rd, "selected_action")
    assert hasattr(rd, "errors")


# ---------------------------------------------------------------------------
# Issue #2607: already_satisfied terminal route -- regression coverage for
# this fixture-driven consumer file (AC11 second VC references this exact
# file). No new .yml fixtures are added (not in this Issue's Allowed Paths);
# cases are constructed inline, matching the style already used above by
# test_conflict_precedes_request_changes() et al.
# ---------------------------------------------------------------------------

_SHA_A = "1" * 40
_SHA_B = "2" * 40


def _already_satisfied_main_drift(current_base_sha: str = _SHA_B) -> dict:
    return {
        "current_base_sha": current_base_sha,
        "evidence_base_sha": current_base_sha,
        "allowed_paths_snapshot_base_sha": current_base_sha,
        "allowed_paths": ["some/path.py"],
        "latest_main_net_diff": [],
        "expected_old_sha": _SHA_A,
        "observed_old_sha": _SHA_A,
    }


def test_already_satisfied_route_constant_exported():
    assert ROUTE_ALREADY_SATISFIED == "already_satisfied"


def test_already_satisfied_reachable_from_request_changes_with_evidence():
    reviewer_verdict = {
        "verdict": "REQUEST_CHANGES",
        "reviewed_head_sha": _SHA_A,
        "blockers": ["addressed by current main already"],
    }
    live_mergeability = {
        "head_sha": _SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "already_satisfied_evidence": {
            "base_ac_satisfied": True,
            "meaningful_pr_delta": False,
            "evidence_base_sha": _SHA_B,
        },
        "main_drift": _already_satisfied_main_drift(),
    }
    result = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
    assert result.route == ROUTE_ALREADY_SATISFIED
    assert result.fail_closed is False
    assert result.selected_action["recommendation"]["pr"]["action"] == "close"


def test_pre_2607_request_changes_fixtures_unaffected_by_already_satisfied():
    """Regression: existing REQUEST_CHANGES fixtures (no
    already_satisfied_evidence key at all) must still route continue_loop,
    byte-identical to pre-#2607 behavior."""
    fx = _load_fixture_by_name("positive_continue_loop.yml")
    result = route_loop_verdict_v2(fx["reviewer_verdict"], fx["live_mergeability"])
    assert result.route == "continue_loop"
    assert result.route != ROUTE_ALREADY_SATISFIED
