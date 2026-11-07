"""I-4-4C4: real `git merge-tree` oracle behind the existing Step 5 router (#87).

Every test drives the real `scripts/agent-ops/pr_head_replay_publish_exec.py` (no stand-in, no
monkeypatched oracle) against throw-away Git repositories under `tmp_path`.  Nothing here calls
GitHub, replays, commits to a repository other than the temporary ones, or pushes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "scripts" / "agent-ops" / "pr_head_replay_publish_exec.py"
IRL = ROOT / ".claude" / "skills" / "impl-review-loop"
ROUTER = IRL / "scripts" / "route_loop_verdict_v2.py"
B1_TERMINAL_GATE_TEST = IRL / "tests" / "test_independent_vc_terminal_gate_regression.py"
ORACLE_MODULE = "pr_head_replay_publish_exec"
HEAD = "a" * 40
ZERO_SHA = "0" * 40


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def router() -> Any:
    return _load(ROUTER, "route_loop_verdict_v2_for_i4_4c4")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init(repo: Path) -> str:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    (repo / "g.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return _git(repo, "symbolic-ref", "--short", "HEAD")


def _two_branches(repo: Path, *, conflicting: bool) -> tuple[str, str]:
    """Return (current_main_sha, candidate_sha).  Both edit f.txt only when `conflicting`."""
    main = _init(repo)
    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "f.txt").write_text("candidate edit\n")
    _git(repo, "commit", "-q", "-am", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", main)
    (repo / ("f.txt" if conflicting else "g.txt")).write_text("main edit\n")
    _git(repo, "commit", "-q", "-am", "main")
    return _git(repo, "rev-parse", "HEAD"), candidate


def _approve() -> dict[str, Any]:
    return {"verdict": "APPROVE", "reviewed_head_sha": HEAD, "blockers": [], "warnings": []}


def _drifted(current: str, candidate: str) -> dict[str, Any]:
    return {
        "head_sha": HEAD,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "main_drift": {
            # `semantic_ambiguity` is omitted on purpose: the router must resolve it itself.
            "current_base_sha": current,
            "evidence_base_sha": candidate,
            "allowed_paths_snapshot_base_sha": current,
            "allowed_paths": ["f.txt", "g.txt"],
            "latest_main_net_diff": ["g.txt"],
            "expected_old_sha": current,
            "observed_old_sha": current,
        },
    }


def _repo_snapshot(repo: Path) -> dict[str, str]:
    index = repo / ".git" / "index"
    return {
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "symbolic-ref", "HEAD"),
        "refs": _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "status": _git(repo, "status", "--porcelain=v1"),
        "index": hashlib.sha256(index.read_bytes()).hexdigest(),
        "worktree": hashlib.sha256(
            (repo / "f.txt").read_bytes() + (repo / "g.txt").read_bytes()
        ).hexdigest(),
    }


# --- the real router -> the real oracle -------------------------------------------------------


def test_router_conflicting_branches_fail_closed_with_the_real_oracle(router, tmp_path):
    current, candidate = _two_branches(tmp_path / "repo", conflicting=True)

    decision = router.route_loop_verdict_v2_resolve_semantic_ambiguity(
        _approve(), _drifted(current, candidate), cwd=tmp_path / "repo"
    )

    assert decision.route == "fail_closed" and decision.fail_closed is True
    assert decision.reason_code == "semantic_ambiguity"
    # The wrapper loaded the migrated file itself, not a stand-in.
    oracle = sys.modules[ORACLE_MODULE]
    assert Path(oracle.__file__).resolve() == ORACLE
    assert Path(oracle.compute_semantic_ambiguity.__code__.co_filename).resolve() == ORACLE


def test_router_non_conflicting_branches_proceed_to_reconciliation(router, tmp_path):
    current, candidate = _two_branches(tmp_path / "repo", conflicting=False)

    decision = router.route_loop_verdict_v2_resolve_semantic_ambiguity(
        _approve(), _drifted(current, candidate), cwd=tmp_path / "repo"
    )

    assert decision.fail_closed is False
    assert decision.route == "route_scope_clean_reconciliation"
    assert decision.reason_code == "main_drift_scope_clean"


def test_router_oracle_missing_fails_closed_never_non_conflict(router, tmp_path, monkeypatch):
    current, candidate = _two_branches(tmp_path / "repo", conflicting=False)
    monkeypatch.setitem(sys.modules, ORACLE_MODULE, None)  # `import` raises ImportError

    decision = router.route_loop_verdict_v2_resolve_semantic_ambiguity(
        _approve(), _drifted(current, candidate), cwd=tmp_path / "repo"
    )

    assert decision.route == "fail_closed" and decision.fail_closed is True
    assert decision.reason_code == "semantic_ambiguity_oracle_unavailable"


def test_router_git_error_is_not_reported_as_a_non_conflict(router, tmp_path):
    current, candidate = _two_branches(tmp_path / "repo", conflicting=False)
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    decision = router.route_loop_verdict_v2_resolve_semantic_ambiguity(
        _approve(), _drifted(current, candidate), cwd=not_a_repo
    )

    assert decision.route == "fail_closed" and decision.reason_code == "semantic_ambiguity"


def test_router_explicit_boolean_is_not_overridden_by_the_oracle(router, tmp_path):
    current, candidate = _two_branches(tmp_path / "repo", conflicting=True)
    live = _drifted(current, candidate)
    live["main_drift"]["semantic_ambiguity"] = False  # caller-supplied: oracle is not consulted

    decision = router.route_loop_verdict_v2_resolve_semantic_ambiguity(
        _approve(), live, cwd=tmp_path / "does-not-exist"
    )

    assert decision.route == "route_scope_clean_reconciliation"


# --- the oracle itself ------------------------------------------------------------------------


def _public_oracle() -> Any:
    return _load(ORACLE, "pr_head_replay_publish_exec_for_i4_4c4")


def test_oracle_conflict_true_and_non_conflict_false(tmp_path):
    oracle = _public_oracle()
    current, candidate = _two_branches(tmp_path / "conflict", conflicting=True)
    assert oracle.compute_semantic_ambiguity(current, candidate, cwd=tmp_path / "conflict") is True
    current, candidate = _two_branches(tmp_path / "clean", conflicting=False)
    assert oracle.compute_semantic_ambiguity(current, candidate, cwd=tmp_path / "clean") is False


@pytest.mark.parametrize("bad_ref", ["no-such-ref", ZERO_SHA, "HEAD~99", ""])
def test_oracle_invalid_ref_is_never_a_non_conflict(tmp_path, bad_ref):
    oracle = _public_oracle()
    current, _ = _two_branches(tmp_path / "repo", conflicting=False)
    assert oracle.compute_semantic_ambiguity(current, bad_ref, cwd=tmp_path / "repo") is True
    assert oracle.compute_semantic_ambiguity(bad_ref, current, cwd=tmp_path / "repo") is True


def test_oracle_non_git_directory_is_never_a_non_conflict(tmp_path):
    oracle = _public_oracle()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert oracle.compute_semantic_ambiguity("a" * 40, "b" * 40, cwd=plain) is True


@pytest.mark.parametrize("option_like", ["--name-only", "-x", "--merge-base=HEAD", "--stdin"])
def test_oracle_dash_prefixed_ref_is_never_a_non_conflict(tmp_path, option_like):
    oracle = _public_oracle()
    current, _ = _two_branches(tmp_path / "repo", conflicting=False)
    spawned: list[Any] = []

    def spy(argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("Git must not be started for an option-like ref")

    for base, candidate in ((option_like, current), (current, option_like)):
        assert oracle.compute_semantic_ambiguity(base, candidate, cwd=tmp_path / "repo") is True
        assert (
            oracle.compute_semantic_ambiguity(base, candidate, cwd=tmp_path / "repo", runner=spy)
            is True
        )
    assert spawned == []


def test_oracle_nonexistent_cwd_raises_never_false(tmp_path):
    oracle = _public_oracle()
    with pytest.raises(OSError):
        oracle.compute_semantic_ambiguity("a" * 40, "b" * 40, cwd=tmp_path / "missing")


@pytest.mark.parametrize(
    ("returncode", "expected"), [(0, False), (1, True), (2, True), (128, True), (-9, True)]
)
def test_oracle_only_exit_zero_means_no_conflict(returncode, expected):
    oracle = _public_oracle()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, returncode, "", "")

    cwd = Path("/nonexistent")
    assert oracle.compute_semantic_ambiguity("base", "cand", cwd=cwd, runner=runner) is expected
    # Exactly one Git command, and it is the documented two-ref merge-tree form.
    assert [argv for argv, _ in calls] == [["git", "merge-tree", "--write-tree", "base", "cand"]]
    assert calls[0][1] == {"cwd": cwd}


def test_oracle_runner_failure_propagates_instead_of_reporting_no_conflict():
    oracle = _public_oracle()

    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(subprocess.TimeoutExpired):
        oracle.compute_semantic_ambiguity("a", "b", cwd=Path("."), runner=runner)


@pytest.mark.parametrize("conflicting", [True, False])
def test_oracle_call_leaves_head_index_worktree_and_refs_unchanged(tmp_path, conflicting):
    oracle = _public_oracle()
    repo = tmp_path / "repo"
    current, candidate = _two_branches(repo, conflicting=conflicting)
    before = _repo_snapshot(repo)

    oracle.compute_semantic_ambiguity(current, candidate, cwd=repo)

    # merge-tree may write tree objects, so only the object database is allowed to grow.
    assert _repo_snapshot(repo) == before


# --- B1 deferred test restored without touching B1's files ------------------------------------

_b1 = _load(B1_TERMINAL_GATE_TEST, "b1_terminal_gate_for_i4_4c4")
mod = _b1.mod
HEAD_A = _b1.HEAD_A
BODY_A = _b1.BODY_A
ALLOWED_PATHS = _b1.ALLOWED_PATHS
ISSUE_NUMBER = _b1.ISSUE_NUMBER
PR_NUMBER = _b1.PR_NUMBER
_hashes = _b1._hashes
_test_verdict = _b1._test_verdict
_contract_snapshot = _b1._contract_snapshot
_diff_summary = _b1._diff_summary
_reviewer_verdict = _b1._reviewer_verdict
_live_mergeability = _b1._live_mergeability


# Verbatim copy of loop-protocol@6fa07b6 test_independent_vc_terminal_gate_regression.py
# ::test_terminal_gate_resolves_semantic_ambiguity (AST-identical; pinned in the C4 manifest).
# Its helper names resolve to the already-migrated B1 test module above.
def test_terminal_gate_resolves_semantic_ambiguity(tmp_path, monkeypatch):
    sha = "0" * 40

    def _git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    branch = _git(repo, "symbolic-ref", "--short", "HEAD")
    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "f.txt").write_text("candidate edit\n")
    _git(repo, "commit", "-q", "-am", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", branch)
    (repo / "f.txt").write_text("main edit\n")
    _git(repo, "commit", "-q", "-am", "main")
    current_sha = _git(repo, "rev-parse", "HEAD")
    assert sha not in (candidate_sha, current_sha)

    live = _live_mergeability(HEAD_A)
    live["main_drift"] = {
        "current_base_sha": current_sha,
        "evidence_base_sha": candidate_sha,
        "allowed_paths_snapshot_base_sha": current_sha,
        "allowed_paths": ["f.txt"],
        "latest_main_net_diff": ["f.txt"],
        "expected_old_sha": current_sha,
        "observed_old_sha": current_sha,
        # `semantic_ambiguity` is intentionally omitted: it must be resolved
        # from the real git oracle, never guessed or fixed by the caller.
    }
    state: dict[str, Any] = {}
    exit_code, _ = mod.step4_adjudicate(
        state,
        expected_head_sha=HEAD_A,
        expected_contract_body_sha256=BODY_A,
        expected_command_hashes=_hashes(False),
        test_verdict=_test_verdict(HEAD_A, BODY_A, False),
        contract_snapshot=_contract_snapshot(BODY_A, False),
        diff_summary=_diff_summary(HEAD_A),
        allowed_paths=ALLOWED_PATHS,
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )
    assert exit_code == 0
    kwargs = dict(  # noqa: C408 - verbatim source line
        expected_head_sha=HEAD_A,
        expected_contract_body_sha256=BODY_A,
        expected_command_hashes=_hashes(False),
        dispatch_seq=1,
    )

    exit_code, term = mod.step5_terminal_gate(
        state, _reviewer_verdict(HEAD_A), live, cwd=repo, **kwargs
    )
    assert exit_code == 1
    assert term["route"] == "fail_closed" and term["reason_code"] == "semantic_ambiguity"

    # Without an explicit cwd the production repository root is the oracle cwd
    # (the CLI path), through the existing public wrapper only.
    route_module = mod._load_route_module()
    seen: dict[str, Any] = {}
    real_wrapper = route_module.route_loop_verdict_v2_resolve_semantic_ambiguity

    def _spy(reviewer_verdict: Any, live_mergeability: Any, *, cwd: Any) -> Any:
        seen["cwd"] = cwd
        return real_wrapper(reviewer_verdict, live_mergeability, cwd=cwd)

    monkeypatch.setattr(route_module, "route_loop_verdict_v2_resolve_semantic_ambiguity", _spy)
    exit_code, term = mod.step5_terminal_gate(
        state, _reviewer_verdict(HEAD_A), _live_mergeability(HEAD_A), **kwargs
    )
    assert exit_code == 0 and term["route"] == "approved"
    assert Path(seen["cwd"]) == mod._REPO_ROOT
