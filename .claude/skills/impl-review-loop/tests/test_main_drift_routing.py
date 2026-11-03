from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from route_loop_verdict_v2 import (  # noqa: E402
    ROUTE_FAIL_CLOSED,
    ROUTE_SCOPE_CLEAN_RECONCILIATION,
    build_step5_live_mergeability,
    route_loop_verdict_v2,
    route_loop_verdict_v2_resolve_semantic_ambiguity,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _verdict() -> dict[str, object]:
    return {
        "verdict": "APPROVE",
        "reviewed_head_sha": SHA_A,
        "blockers": [],
        "warnings": [],
    }


def _live(
    merge_state_status: str,
    main_drift: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "head_sha": SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": merge_state_status,
    }
    value["main_drift"] = _fresh_drift() if main_drift is None else main_drift
    return value


def _live_without_main_drift(merge_state_status: str) -> dict[str, object]:
    """main_drift remains an optional key on the public routing boundary."""
    return {
        "head_sha": SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": merge_state_status,
    }


def _drift(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "current_base_sha": SHA_B,
        "evidence_base_sha": SHA_C,
        "allowed_paths_snapshot_base_sha": SHA_B,
        "allowed_paths": [".claude/skills/impl-review-loop/"],
        "latest_main_net_diff": [
            ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
        ],
        "expected_old_sha": SHA_B,
        "observed_old_sha": SHA_B,
    }
    value.update(extra)
    return value


def _fresh_drift(**extra: object) -> dict[str, object]:
    value = _drift(evidence_base_sha=SHA_B)
    value.update(extra)
    return value


def test_given_scope_clean_drift_when_routed_then_reconciliation_reverifies_without_spending_iteration():
    decision = route_loop_verdict_v2(_verdict(), _live("BEHIND", _drift()))

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.rerun_required == {"snapshot": True, "ci": True, "review": True}
    assert decision.selected_action["evidence_epoch"]["implementation_iteration_delta"] == 0


def test_given_eligible_behind_drift_when_routed_then_fast_path_avoids_reconciliation():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(behind_fast_path_eligible=True)),
    )

    assert decision.route == "route_to_update_branch"
    assert decision.selected_action["main_drift_strategy"] == "behind_fast_path"


@pytest.mark.parametrize("merge_state_status", ["CLEAN", "HAS_HOOKS", "BLOCKED", "DRAFT", "UNSTABLE"])
def test_given_scope_clean_drift_in_any_nonbehind_state_when_routed_then_old_evidence_is_invalidated(
    merge_state_status: str,
):
    decision = route_loop_verdict_v2(
        _verdict(),
        _live(merge_state_status, _drift(behind_fast_path_eligible=True)),
    )

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.selected_action["reusable_evidence"] == {
        "snapshot": None,
        "ci": None,
        "review": None,
    }
    assert decision.selected_action["evidence_epoch"]["base_sha"] == SHA_B
    assert decision.selected_action["evidence_epoch"]["implementation_iteration_delta"] == 0


@pytest.mark.parametrize("merge_state_status", ["CLEAN", "HAS_HOOKS", "BLOCKED", "DRAFT", "UNSTABLE"])
def test_given_step5_control_plane_live_facts_when_stale_evidence_exists_then_nonbehind_state_cannot_bypass_rebind(
    merge_state_status: str,
):
    """Step 5 の production input 経路は stale evidence を必ず router に渡す。"""
    live_mergeability = build_step5_live_mergeability(
        _live(merge_state_status),
        _drift(behind_fast_path_eligible=True),
    )

    decision = route_loop_verdict_v2(_verdict(), live_mergeability)

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.route != "approved"
    assert decision.selected_action["reusable_evidence"] == {
        "snapshot": None,
        "ci": None,
        "review": None,
    }
    assert decision.rerun_required == {"snapshot": True, "ci": True, "review": True}


def test_given_missing_main_drift_when_approve_then_pre_2102_routing_is_preserved():
    """main_drift is an optional key (Design Invariant: the public
    two-input routing boundary is stable). Callers that omit it entirely
    keep the pre-#2102 CLEAN/MERGEABLE -> approved behavior; only a
    PRESENT-but-malformed main_drift payload fails closed."""
    decision = route_loop_verdict_v2(_verdict(), _live_without_main_drift("CLEAN"))

    assert decision.route == "approved"


@pytest.mark.parametrize(
    "main_drift, reason_code",
    [
        ({"current_base_sha": SHA_B}, "main_drift_context_invalid"),
        (_fresh_drift(allowed_paths="not-a-path-list"), "main_drift_context_invalid"),
    ],
)
def test_given_invalid_step5_main_drift_facts_when_approve_then_router_fails_closed(
    main_drift: dict[str, object],
    reason_code: str,
):
    live_mergeability = {
        "head_sha": SHA_A,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "main_drift": main_drift,
    }

    decision = route_loop_verdict_v2(_verdict(), live_mergeability)

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.fail_closed is True
    assert decision.reason_code == reason_code
    assert decision.route != "approved"


def test_given_semantic_ambiguity_when_routed_then_it_stops_without_action():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("CLEAN", _drift(semantic_ambiguity=True)),
    )

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.selected_action is None



def test_given_drift_rebind_attempts_below_budget_when_routed_then_epoch_carries_next_attempt_count():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(drift_rebind_attempts=1)),
    )

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.selected_action["evidence_epoch"]["drift_rebind_attempts"] == 2
    assert decision.selected_action["evidence_epoch"]["max_drift_rebind_attempts"] == 2


def test_given_drift_rebind_attempts_at_budget_when_routed_then_fail_closed_not_human_escalation():
    """Issue #2102 P0-B: bounded outer drift-rebind budget, independent of
    LOOP_STATE.iteration / implementation_iteration_delta. A churning main
    must not livelock the loop, but exhausting the bound is a deterministic
    machine stop, not a semantic-judgment human_escalation route."""
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(drift_rebind_attempts=2)),
    )

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.fail_closed is True
    assert decision.reason_code == "concurrent_base_churn_budget_exhausted"
    assert decision.route != "route_human_escalation"


def test_given_negative_drift_rebind_attempts_when_routed_then_context_invalid():
    decision = route_loop_verdict_v2(
        _verdict(),
        _live("BEHIND", _drift(drift_rebind_attempts=-1)),
    )

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.reason_code == "main_drift_context_invalid"


# ---------------------------------------------------------------------------
# Issue #2102 fix_delta iteration 5, Blocker B: real-subprocess integration
# tests proving route_loop_verdict_v2_resolve_semantic_ambiguity() actually
# invokes scripts/agent-ops/pr_head_replay_publish_exec.py's git merge-tree
# oracle -- never a caller-asserted boolean -- end-to-end in a real git repo.
# ---------------------------------------------------------------------------

class _OracleStandIn:
    """Test-local stand-in for pr_head_replay_publish_exec (NOT migrated in I-4-4A).

    It reproduces only the read-only contract the router wrapper consumes --
    ``compute_semantic_ambiguity(base, candidate, *, cwd)`` == ``git merge-tree
    --write-tree`` exits non-zero -- using a REAL git subprocess, so the wrapper's
    call path is exercised end-to-end.  It is a contract test of the wrapper, not
    a parity test of the source oracle (owner: I-4-4C).
    """

    calls: list[tuple[str, str]] = []

    @staticmethod
    def compute_semantic_ambiguity(base_sha, candidate_sha, *, cwd, runner=None):
        _OracleStandIn.calls.append((base_sha, candidate_sha))
        proc = subprocess.run(
            ["git", "merge-tree", "--write-tree", base_sha, candidate_sha],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode != 0


@pytest.fixture
def oracle_stand_in(monkeypatch):
    import types

    _OracleStandIn.calls.clear()
    mod = types.ModuleType("pr_head_replay_publish_exec")
    mod.compute_semantic_ambiguity = _OracleStandIn.compute_semantic_ambiguity
    monkeypatch.setitem(sys.modules, "pr_head_replay_publish_exec", mod)


@pytest.fixture
def oracle_absent(monkeypatch):
    monkeypatch.delitem(sys.modules, "pr_head_replay_publish_exec", raising=False)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "f.txt").write_text("base\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    return repo


def _rev_parse(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_given_real_conflicting_git_state_when_wrapper_resolves_ambiguity_then_it_hard_stops(tmp_path, oracle_stand_in):
    """A genuine two-sided edit of the SAME file (real git merge-tree
    conflict, not an asserted flag) must resolve semantic_ambiguity=True via
    the real oracle and hard_stop -- proving route_loop_verdict_v2.py's
    convenience wrapper actually reaches
    pr_head_replay_publish_exec.compute_semantic_ambiguity() in production,
    not merely at unit-test granularity."""
    repo = _init_repo(tmp_path)
    original_branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    _git(["checkout", "-q", "-b", "branch-edit"], repo)
    (repo / "f.txt").write_text("branch edit\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "branch edit"], repo)
    candidate_sha = _rev_parse(repo)

    _git(["checkout", "-q", original_branch], repo)
    (repo / "f.txt").write_text("main edit\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "main edit"], repo)
    current_sha = _rev_parse(repo)

    live_mergeability = _live(
        "CLEAN",
        _drift(
            current_base_sha=current_sha,
            evidence_base_sha=candidate_sha,
            allowed_paths_snapshot_base_sha=current_sha,
            latest_main_net_diff=["f.txt"],
            allowed_paths=["f.txt"],
            expected_old_sha=current_sha,
            observed_old_sha=current_sha,
        ),
    )
    decision = route_loop_verdict_v2_resolve_semantic_ambiguity(_verdict(), live_mergeability, cwd=repo)

    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.reason_code == "semantic_ambiguity"
    assert decision.selected_action is None
    # the wrapper must hand the oracle (evidence base, current base) in that order
    assert _OracleStandIn.calls == [(candidate_sha, current_sha)]


def test_given_real_nonconflicting_git_state_when_wrapper_resolves_ambiguity_then_reconciliation_proceeds(tmp_path, oracle_stand_in):
    """A genuine non-conflicting net-new commit must resolve
    semantic_ambiguity=False via the real oracle and route to scope-clean
    reconciliation instead of hard-stopping."""
    repo = _init_repo(tmp_path)
    evidence_sha = _rev_parse(repo)

    (repo / "other.txt").write_text("net-new\n")
    _git(["add", "other.txt"], repo)
    _git(["commit", "-q", "-m", "advance"], repo)
    current_sha = _rev_parse(repo)

    live_mergeability = _live(
        "CLEAN",
        _drift(
            current_base_sha=current_sha,
            evidence_base_sha=evidence_sha,
            allowed_paths_snapshot_base_sha=current_sha,
            latest_main_net_diff=["other.txt"],
            allowed_paths=["other.txt", "f.txt"],
            expected_old_sha=current_sha,
            observed_old_sha=current_sha,
        ),
    )
    decision = route_loop_verdict_v2_resolve_semantic_ambiguity(_verdict(), live_mergeability, cwd=repo)

    assert decision.route == ROUTE_SCOPE_CLEAN_RECONCILIATION
    assert decision.reason_code == "main_drift_scope_clean"


def test_given_explicit_semantic_ambiguity_when_wrapper_called_then_no_subprocess_needed(tmp_path):
    """When the caller already supplied semantic_ambiguity explicitly, the
    wrapper must not perform any additional git probe -- it delegates
    byte-identically to route_loop_verdict_v2(). A nonexistent cwd path
    proves no subprocess was attempted."""
    nonexistent_cwd = tmp_path / "does-not-exist"

    direct = route_loop_verdict_v2(_verdict(), _live("BEHIND", _drift(semantic_ambiguity=False)))
    wrapped = route_loop_verdict_v2_resolve_semantic_ambiguity(
        _verdict(), _live("BEHIND", _drift(semantic_ambiguity=False)), cwd=nonexistent_cwd
    )

    assert wrapped.route == direct.route
    assert wrapped.selected_action == direct.selected_action


# ---------------------------------------------------------------------------
# I-4-4A additions (not in the source tests): the merge-tree oracle and the
# landing-evidence producer are NOT migrated yet.  An unavailable dependency
# must fail closed -- never approve, never reconcile, never assume "no conflict".
# ---------------------------------------------------------------------------


def _drifted_live(sha_current: str, sha_evidence: str) -> dict[str, object]:
    return _live(
        "CLEAN",
        {
            "current_base_sha": sha_current,
            "evidence_base_sha": sha_evidence,
            "allowed_paths_snapshot_base_sha": sha_current,
            "allowed_paths": ["f.txt"],
            "latest_main_net_diff": ["f.txt"],
            "expected_old_sha": sha_current,
            "observed_old_sha": sha_current,
        },
    )


def test_given_unmigrated_oracle_when_ambiguity_unresolved_then_fail_closed(tmp_path, oracle_absent):
    assert not (REPO_ROOT / "scripts" / "agent-ops" / "pr_head_replay_publish_exec.py").exists()
    decision = route_loop_verdict_v2_resolve_semantic_ambiguity(
        _verdict(), _drifted_live(SHA_B, SHA_C), cwd=tmp_path
    )
    assert decision.route == ROUTE_FAIL_CLOSED
    assert decision.fail_closed is True
    assert decision.reason_code == "semantic_ambiguity_oracle_unavailable"
    assert decision.selected_action is None


def test_given_unmigrated_oracle_when_ambiguity_is_explicit_then_no_oracle_needed(tmp_path, oracle_absent):
    live = _drifted_live(SHA_B, SHA_C)
    live["main_drift"]["semantic_ambiguity"] = False
    wrapped = route_loop_verdict_v2_resolve_semantic_ambiguity(_verdict(), live, cwd=tmp_path)
    assert wrapped.route == route_loop_verdict_v2(_verdict(), live).route == ROUTE_SCOPE_CLEAN_RECONCILIATION


def test_given_oracle_import_failure_when_fresh_evidence_then_oracle_is_not_consulted(tmp_path, oracle_absent):
    live = _drifted_live(SHA_B, SHA_B)  # current == evidence: fresh, no ambiguity to resolve
    decision = route_loop_verdict_v2_resolve_semantic_ambiguity(_verdict(), live, cwd=tmp_path)
    assert decision.route == route_loop_verdict_v2(_verdict(), live).route


def test_given_unmigrated_landing_evidence_producer_when_disposition_resolved_then_fail_closed(tmp_path):
    # I-4-4C1: the producer is now migrated, so the missing-producer branch is exercised on a
    # copied layout (router + drift policy only; the sibling producer is deliberately absent).
    import importlib.util
    import shutil

    root = Path(__file__).resolve().parents[4]
    irl = tmp_path / ".claude/skills/impl-review-loop/scripts"
    irl.mkdir(parents=True)
    shutil.copy(SCRIPTS / "route_loop_verdict_v2.py", irl)
    (tmp_path / "scripts/agent-ops").mkdir(parents=True)
    shutil.copy(root / "scripts/agent-ops/main_drift_policy.py", tmp_path / "scripts/agent-ops")
    spec = importlib.util.spec_from_file_location("route_copy_without_producer", irl / "route_loop_verdict_v2.py")
    assert spec is not None and spec.loader is not None
    copy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = copy
    spec.loader.exec_module(copy)
    resolve_pre_step1_data_plane_action = copy.resolve_pre_step1_data_plane_action
    resolve_pre_step1_landing_disposition = copy.resolve_pre_step1_landing_disposition

    assert not (irl / "implementation_landed_evidence.py").exists()
    disposition = resolve_pre_step1_landing_disposition({}, repo="squne121/agent-platform", issue_number=1)
    assert disposition["disposition"] == "reconciliation_required"
    assert disposition["reason_codes"] == ["implementation_landed_evidence_module_unavailable"]
    assert disposition["candidate"] is None
    action = resolve_pre_step1_data_plane_action(disposition)
    assert action["start_data_plane"] is False
    assert action["action"] == "suppress_worker_worktree_new_pr"
    # an unresolved / foreign disposition may never start the data plane
    for bogus in ({}, {"disposition": "already_satisfied"}, None, "ordinary_dispatch_or_explicit_recovery"):
        assert resolve_pre_step1_data_plane_action(bogus)["start_data_plane"] is False
