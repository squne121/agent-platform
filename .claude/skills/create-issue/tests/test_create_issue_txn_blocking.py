"""Unit tests for the --blocked-by/--blocking direction fix in create_issue_txn.py (#1946).

Test function names below are referenced directly (bare, module-level, no class) by the
Issue's Verification Commands (e.g. ``pytest ...::test_blocking_and_blocked_by_parse_to_separate_destinations``),
so they are intentionally NOT wrapped in test classes.

Covers:
- AC1: create/reconcile parsers parse --blocked-by/--blocking into separate destinations
- AC2: exact-argv GraphQL addBlockedBy mutation input mapping for both directions
- AC3: invalid dependency inputs (non-positive/bool/duplicate/cross-direction/self/cross-repo)
       are rejected before any mutation
- AC4: direction-specific readback with strict set match + pageInfo/totalCount + target-side
       cross-check for --blocking
- AC5: create/dedupe/reconcile/partial-failure/recovery-hint all handle both directions;
       TransactionResult exposes direction-specific verification results
- AC6: official `gh issue create --blocked-by`/`--blocking` flags are never forwarded to
       _issue_create
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
_LIVE_CANARY_SCRIPT = Path(__file__).parent / "live_canary_blocking_direction.sh"

import create_issue_txn as txn  # noqa: E402


class FakeSleep:
    """Records sleep calls without actually sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def _make_gh_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# AC1: parser destinations
# ---------------------------------------------------------------------------


def test_blocking_and_blocked_by_parse_to_separate_destinations() -> None:
    ns = txn.parse_args(
        [
            "--repo",
            "owner/repo",
            "--title",
            "t",
            "--blocked-by",
            "10",
            "--blocking",
            "20",
            "--blocking",
            "21",
        ]
    )
    assert ns.dependency == [10]
    assert ns.blocking == [20, 21]

    # --dependency remains a legacy alias absorbed into the same destination as --blocked-by.
    ns2 = txn.parse_args(["--repo", "owner/repo", "--title", "t", "--dependency", "5"])
    assert ns2.dependency == [5]
    assert ns2.blocking == []

    # reconcile parser also exposes --blocking as a distinct destination.
    ns3 = txn.parse_args(
        [
            "reconcile",
            "--repo",
            "owner/repo",
            "--issue",
            "99",
            "--dependency",
            "7",
            "--blocking",
            "8",
        ]
    )
    assert ns3.subcommand == "reconcile"
    assert ns3.dependency == [7]
    assert ns3.blocking == [8]


# ---------------------------------------------------------------------------
# AC2: exact-argv GraphQL mutation input mapping
# ---------------------------------------------------------------------------


def test_blocking_direction_exact_argv_mutation_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """--blocking: issueId=<target>, blockingIssueId=<new issue> (new issue blocks target)."""
    captured: dict[str, Any] = {}

    def _fake_run_gh_text(args: list[str], *, stage: str) -> str:
        captured["args"] = args
        captured["stage"] = stage
        return "ok"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text)
    txn._issue_register_blocking("owner/repo", "NEW_ISSUE_NODE", "TARGET_NODE", "gh")

    args = captured["args"]
    assert captured["stage"] == "blocking-register"
    assert "input[issueId]=TARGET_NODE" in args
    assert "input[blockingIssueId]=NEW_ISSUE_NODE" in args
    assert any("addBlockedBy" in a for a in args if a.startswith("query="))


def test_blocked_by_direction_exact_argv_mutation_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """--blocked-by: issueId=<new issue>, blockingIssueId=<dependency> (new issue is blocked)."""
    captured: dict[str, Any] = {}

    def _fake_run_gh_text(args: list[str], *, stage: str) -> str:
        captured["args"] = args
        captured["stage"] = stage
        return "ok"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text)
    txn._issue_register_dependency("owner/repo", "NEW_ISSUE_NODE", "DEP_NODE", "gh")

    args = captured["args"]
    assert captured["stage"] == "dependency-register"
    assert "input[issueId]=NEW_ISSUE_NODE" in args
    assert "input[blockingIssueId]=DEP_NODE" in args

    # Both directions must share the identical addBlockedBy mutation query (only the
    # role-fixed keyword args swap; no separate mutation/query per direction).
    queries: list[str] = []

    def _fake_run_gh_text_2(args2: list[str], *, stage: str) -> str:
        for a in args2:
            if a.startswith("query="):
                queries.append(a)
        return "ok"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text_2)
    txn._issue_register_dependency("owner/repo", "A", "B", "gh")
    txn._issue_register_blocking("owner/repo", "A", "B", "gh")
    assert len(queries) == 2
    assert queries[0] == queries[1]


# ---------------------------------------------------------------------------
# AC3: reject invalid inputs before any mutation
# ---------------------------------------------------------------------------


def test_invalid_dependency_inputs_rejected_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-positive integers (0, negative) rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers([0], label="--blocked-by")
    assert exc_info.value.stage == "dependency-parse"

    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers([-5], label="--blocking")
    assert exc_info.value.stage == "dependency-parse"

    # Bool (a subclass of int in Python) rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers([True], label="--blocked-by")
    assert exc_info.value.stage == "dependency-parse"

    # Cross-repository issue URL rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers(["https://github.com/other/repo/issues/5"], label="--blocking")
    assert exc_info.value.stage == "dependency-parse"

    # Duplicate issue numbers within a single direction rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([5, 5], [])
    assert exc_info.value.stage == "dependency-validate"

    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([], [6, 6])
    assert exc_info.value.stage == "dependency-validate"

    # Same issue specified in both directions rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([7], [7])
    assert exc_info.value.stage == "dependency-validate"

    # Dedupe-target self-reference rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([1, 2], [], self_issue_number=2)
    assert exc_info.value.stage == "dependency-validate"

    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([], [9], self_issue_number=9)
    assert exc_info.value.stage == "dependency-validate"

    # End-to-end: run_transaction must fail closed (status=failure) without ever
    # calling gh when the input is invalid.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no GitHub mutation should be attempted for invalid input")

    monkeypatch.setattr(txn, "run_command", _boom)
    monkeypatch.setattr(txn, "_run_gh_json", _boom)
    monkeypatch.setattr(txn, "_run_gh_text", _boom)

    result = txn._run_transaction_engine(
        repo="owner/repo",
        title="some title",
        body="body",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[3],
        blocking_issue_numbers=[3],
        gh_bin="gh",
    )
    assert result.status == "failure"
    assert result.failure_stage == "dependency-validate"


# ---------------------------------------------------------------------------
# #1946 Owner P0-2 (required test 2): reconcile_transaction rejects 0/negative/bool
# --blocked-by/--blocking values BEFORE any label/parent/dependency mutation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("invalid_value", [0, -1, True])
def test_reconcile_transaction_rejects_invalid_dependency_before_any_mutation(
    invalid_value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reconcile_transaction must reject 0/negative/bool --blocked-by/--blocking values
    before mutating labels, parent, or dependency/blocking links (#1946 Owner P0-2).

    Previously reconcile_transaction only ran _validate_dependency_directions (duplicate/
    self-reference/cross-direction checks), never _normalize_dependency_numbers, so an
    input like ``--blocking 0`` would reach label mutation before failing while resolving
    issue #0's node id.
    """

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"no GitHub mutation should be attempted for invalid input {invalid_value!r}")

    monkeypatch.setattr(txn, "run_command", _boom)
    monkeypatch.setattr(txn, "_run_gh_json", _boom)
    monkeypatch.setattr(txn, "_run_gh_text", _boom)
    monkeypatch.setattr(txn, "_issue_apply_labels", _boom)
    monkeypatch.setattr(txn, "_issue_register_sub_issue_idempotent", _boom)
    monkeypatch.setattr(txn, "_issue_register_dependency", _boom)
    monkeypatch.setattr(txn, "_issue_register_blocking", _boom)
    monkeypatch.setattr(txn, "_issue_graphql_ids", _boom)

    result = txn._reconcile_transaction_engine(
        repo="owner/repo",
        issue_number=99,
        labels=["x"],
        parent_issue_number=40,
        dependency_issue_numbers=[invalid_value],
        gh_bin="gh",
    )
    assert result.status == "failure"
    assert result.failure_stage == "dependency-parse"

    result_blocking = txn._reconcile_transaction_engine(
        repo="owner/repo",
        issue_number=99,
        labels=["x"],
        parent_issue_number=40,
        dependency_issue_numbers=[],
        blocking_issue_numbers=[invalid_value],
        gh_bin="gh",
    )
    assert result_blocking.status == "failure"
    assert result_blocking.failure_stage == "dependency-parse"


# ---------------------------------------------------------------------------
# AC4: direction-specific strict readback
# ---------------------------------------------------------------------------


def test_readback_direction_strict_set_match_and_pageinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    # --- blocked-by direction ---
    stdout_ok = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":2,"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"number":10},{"number":20}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_ok))
    assert txn._readback_dependencies("owner/repo", 99, [10, 20], "gh") is True

    # Strict set equality: an extra unrequested blockedBy entry must NOT pass.
    stdout_superset = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":2,"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"number":10},{"number":999}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_superset))
    assert txn._readback_dependencies("owner/repo", 99, [10], "gh") is False

    # hasNextPage: True must not pass even if the visible nodes match.
    stdout_paginated = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":1,"pageInfo":{"hasNextPage":true},'
        '"nodes":[{"number":10}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_paginated))
    assert txn._readback_dependencies("owner/repo", 99, [10], "gh") is False

    # totalCount mismatch must not pass.
    stdout_count_mismatch = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":5,"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"number":10}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_count_mismatch))
    assert txn._readback_dependencies("owner/repo", 99, [10], "gh") is False

    # --- blocking direction (with target-side cross-check) ---
    calls: list[list[str]] = []

    def _fake_run_pass(args: list[str], **_k: Any) -> Any:
        calls.append(args)
        if len(calls) == 1:
            return _make_gh_result(
                stdout=(
                    '{"data":{"repository":{"issue":{"blocking":'
                    '{"totalCount":1,"pageInfo":{"hasNextPage":false},'
                    '"nodes":[{"number":50}]}}}}}'
                )
            )
        return _make_gh_result(
            stdout='{"data":{"repository":{"issue":{"blockedBy":{"nodes":[{"number":99}]}}}}}'
        )

    monkeypatch.setattr(txn, "run_command", _fake_run_pass)
    assert txn._readback_blocking("owner/repo", 99, [50], "gh") is True
    assert len(calls) == 2

    # Target-side blockedBy missing the new issue -> overall False, even though the
    # new issue's own `blocking` connection looked correct.
    calls2: list[list[str]] = []

    def _fake_run_fail_target(args: list[str], **_k: Any) -> Any:
        calls2.append(args)
        if len(calls2) == 1:
            return _make_gh_result(
                stdout=(
                    '{"data":{"repository":{"issue":{"blocking":'
                    '{"totalCount":1,"pageInfo":{"hasNextPage":false},'
                    '"nodes":[{"number":50}]}}}}}'
                )
            )
        return _make_gh_result(stdout='{"data":{"repository":{"issue":{"blockedBy":{"nodes":[]}}}}}')

    monkeypatch.setattr(txn, "run_command", _fake_run_fail_target)
    assert txn._readback_blocking("owner/repo", 99, [50], "gh") is False

    # Own-side mismatch (blocking connection empty) -> False without even reaching the
    # target-side cross-check.
    stdout_own_side_mismatch = (
        '{"data":{"repository":{"issue":{"blocking":'
        '{"totalCount":0,"pageInfo":{"hasNextPage":false},'
        '"nodes":[]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_own_side_mismatch))
    assert txn._readback_blocking("owner/repo", 99, [50], "gh") is False


# ---------------------------------------------------------------------------
# AC5: dedupe/reconcile/partial-failure/recovery-hint bidirectional handling
# ---------------------------------------------------------------------------


def test_dedupe_reconcile_partial_failure_bidirectional(monkeypatch: pytest.MonkeyPatch) -> None:
    # --- reconcile_transaction registers+readbacks the --blocking direction ---
    monkeypatch.setattr(txn, "_issue_apply_labels", lambda *_a, **_k: None)
    monkeypatch.setattr(txn, "_readback_labels", lambda *_a, **_k: True)
    monkeypatch.setattr(
        txn,
        "_readback_labels_with_result",
        lambda *_a, **_k: MagicMock(expected_labels=[], actual_labels=[], attempts=1, retry_delays=[]),
    )
    # #1946 Owner P0-1: the shared apply/readback helper diffs against the CURRENT
    # relationship state before mutating. Simulate "nothing registered yet" so both
    # requested numbers are computed as missing and must be mutated.
    monkeypatch.setattr(txn, "_current_relationship_numbers", lambda *_a, **_k: ("ok", set()))

    register_calls: list[tuple[Any, ...]] = []

    def _spy_register(repo: str, child_node_id: str, target_node_id: str, gh_bin: str) -> None:
        register_calls.append((repo, child_node_id, target_node_id))

    def _spy_graphql_ids(repo: str, issue_number: int, gh_bin: str) -> tuple[str, int]:
        return (f"node-{issue_number}", issue_number * 100)

    readback_calls: list[Any] = []

    def _spy_readback(repo: str, issue_number: int, nums: list[int], gh_bin: str) -> bool:
        readback_calls.append((repo, issue_number, nums))
        return True

    monkeypatch.setattr(txn, "_issue_graphql_ids", _spy_graphql_ids)
    monkeypatch.setattr(txn, "_issue_register_blocking", _spy_register)
    monkeypatch.setattr(txn, "_readback_blocking", _spy_readback)

    fake_sleep = FakeSleep()
    result = txn._reconcile_transaction_engine(
        repo="owner/repo",
        issue_number=99,
        labels=["label1"],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        blocking_issue_numbers=[10, 20],
        gh_bin="gh",
        sleep_fn=fake_sleep,
    )
    assert len(register_calls) == 2, f"Expected 2 register calls, got {register_calls}"
    # Exact GraphQL node ids must be used for both the issue itself and each target.
    assert {c[1] for c in register_calls} == {"node-99"}
    assert {c[2] for c in register_calls} == {"node-10", "node-20"}
    assert len(readback_calls) >= 1, "_readback_blocking must be called at least once"
    assert result.status == "success"
    assert result.blocking_verified is True

    # --- run_transaction (create path): both directions reported independently ---
    monkeypatch.setattr(txn, "_run_issue_body_validator", lambda *_a, **_k: {"status": "pass"})
    monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [])
    monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/500")
    monkeypatch.setattr(txn, "_poll_for_created_issue", lambda *_a, **_k: ("ok", []))
    monkeypatch.setattr(txn, "_issue_register_dependency", lambda *_a, **_k: None)
    monkeypatch.setattr(txn, "_readback_dependencies", lambda *_a, **_k: True)

    fake_sleep2 = FakeSleep()
    result2 = txn._run_transaction_engine(
        repo="owner/repo",
        title="t",
        body="",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[10],
        blocking_issue_numbers=[20],
        gh_bin="gh",
        sleep_fn=fake_sleep2,
    )
    assert result2.status == "success"
    assert result2.issue_number == 500
    assert result2.dependency_verified is True
    assert result2.blocking_verified is True

    # --- run_transaction (create path): blocking readback failure -> partial_failure ---
    # #1946 Owner P1-2: --blocked-by succeeded (dependency_verified stays True from the
    # prior scenario's registration path) while --blocking fails; both directions must be
    # reported independently rather than collapsing to null.
    monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/501")
    monkeypatch.setattr(txn, "_readback_blocking", lambda *_a, **_k: False)
    monkeypatch.setattr(txn, "_post_partial_failure_comment", lambda *_a, **_k: None)

    fake_sleep3 = FakeSleep()
    result3 = txn._run_transaction_engine(
        repo="owner/repo",
        title="t",
        body="",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[11],
        blocking_issue_numbers=[30],
        gh_bin="gh",
        sleep_fn=fake_sleep3,
    )
    assert result3.status == "partial_failure"
    assert result3.failure_stage == "blocking-readback"
    assert result3.dependency_verified is True, "dependency (blocked-by) succeeded and must not be nulled out"
    assert result3.blocking_verified is False, "blocking failed a readback and must be False, not null"

    # --- dedupe path also reconciles the --blocking direction: link missing -> mutation
    # runs -> readback confirms -> status=dedupe (#1946 Owner required test 1 / P0-1) ---
    monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [700])
    monkeypatch.setattr(txn, "_run_gh_json", lambda *_a, **_k: {"body": "", "number": 700})

    dedupe_register_calls: list[tuple[Any, ...]] = []

    def _spy_register_dedupe(repo: str, child_node_id: str, target_node_id: str, gh_bin: str) -> None:
        dedupe_register_calls.append((repo, child_node_id, target_node_id))

    dedupe_readback_attempts: list[int] = []

    def _spy_readback_mismatch_then_match(
        repo: str, issue_number: int, nums: list[int], gh_bin: str
    ) -> bool:
        # First call (before mutation would be observed): mismatch. Second call (after
        # mutation): match. This proves the mutation is what causes the transition, not
        # an unconditional True stub (the exact false-positive Owner flagged).
        dedupe_readback_attempts.append(1)
        return len(dedupe_readback_attempts) > 1

    monkeypatch.setattr(txn, "_issue_register_blocking", _spy_register_dedupe)
    monkeypatch.setattr(txn, "_readback_blocking", _spy_readback_mismatch_then_match)

    fake_sleep4 = FakeSleep()
    result4 = txn._run_transaction_engine(
        repo="owner/repo",
        title="t",
        body="",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        blocking_issue_numbers=[42],
        gh_bin="gh",
        sleep_fn=fake_sleep4,
    )
    assert result4.status == "dedupe"
    assert result4.dedupe_number == 700
    assert result4.blocking_verified is True
    assert len(dedupe_register_calls) == 1, (
        f"dedupe path must actually mutate the missing blocking link, got {dedupe_register_calls}"
    )
    assert dedupe_register_calls[0][2] == "node-42"
    assert len(dedupe_readback_attempts) >= 2, "readback must be retried after mutation, not stubbed unconditionally"

    # --- recovery hint covers the blocking stage ---
    hint = txn._recovery_hint_for_stage("blocking-readback", "owner/repo", 42, 0, [], [99])
    assert "#99" in hint
    assert "input[blockingIssueId]=<new_issue_node_id>" in hint
    assert "\n" in hint


# ---------------------------------------------------------------------------
# AC6: official gh CLI dependency flags must never be forwarded to _issue_create
# ---------------------------------------------------------------------------


def test_issue_create_does_not_forward_dependency_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_run_gh_text(args: list[str], *, stage: str) -> str:
        captured["args"] = args
        return "https://github.com/owner/repo/issues/1"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text)
    txn._issue_create("owner/repo", "title", "body", "", "gh")

    args = captured["args"]
    assert "--blocked-by" not in args
    assert "--blocking" not in args
    assert "--dependency" not in args

    # _issue_create must not even accept a dependency/blocking parameter -- the only way
    # to reach an addBlockedBy mutation is through the separate helpers below it.
    import inspect

    sig = inspect.signature(txn._issue_create)
    names = set(sig.parameters.keys())
    assert "dependency_issue_numbers" not in names
    assert "blocking_issue_numbers" not in names


# ---------------------------------------------------------------------------
# #1946 Owner P1-3 (required tests 4/5): full pagination + null-issue handling
# for the target-side blockedBy cross-check used by the --blocking direction.
# ---------------------------------------------------------------------------


def test_target_blockedby_contains_paginates_past_100_and_finds_second_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required test 4: a target issue with more than 100 existing blockers, where the
    expected issue is only on the SECOND page, must not produce a false negative.

    Regression: the previous _target_blockedby_contains() fetched only blockedBy(first:100)
    with no pageInfo/cursor traversal, so an expected issue past the first 100 nodes
    was silently missed.
    """
    calls: list[list[str]] = []

    def _payload(*, total_count: int, has_next_page: bool, end_cursor: Any, numbers: list[int]) -> str:
        return json.dumps(
            {
                "data": {
                    "repository": {
                        "issue": {
                            "blockedBy": {
                                "totalCount": total_count,
                                "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                                "nodes": [{"number": n} for n in numbers],
                            }
                        }
                    }
                }
            }
        )

    def _fake_run(args: list[str], **_k: Any) -> Any:
        calls.append(args)
        has_after = any(a.startswith("after=") for a in args)
        if not has_after:
            # First page: 100 filler numbers, hasNextPage true.
            return _make_gh_result(
                stdout=_payload(
                    total_count=101, has_next_page=True, end_cursor="CURSOR1", numbers=list(range(1, 101))
                )
            )
        # Second page: the expected issue (number 999) plus hasNextPage false.
        return _make_gh_result(
            stdout=_payload(total_count=101, has_next_page=False, end_cursor=None, numbers=[999])
        )

    monkeypatch.setattr(txn, "run_command", _fake_run)
    assert txn._target_blockedby_contains("owner/repo", 500, 999, "gh") is True
    assert len(calls) == 2, f"expected exactly 2 paginated calls, got {len(calls)}"
    assert any("after=CURSOR1" in a for a in calls[1]), "second call must pass the endCursor from page 1"

    # Same setup, but looking for an issue number that does NOT appear on either page.
    calls.clear()
    monkeypatch.setattr(txn, "run_command", _fake_run)
    assert txn._target_blockedby_contains("owner/repo", 500, 12345, "gh") is False


def test_paginated_relationship_numbers_handles_null_issue_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required test 5: a GraphQL response with `issue: null` (deleted/inaccessible issue)
    must be a structured failure, never an uncaught AttributeError/traceback.
    """
    monkeypatch.setattr(
        txn,
        "run_command",
        lambda *_a, **_k: _make_gh_result(
            stdout='{"data":{"repository":{"issue":null}}}'
        ),
    )

    # Low-level pagination primitive: structured "issue_not_found", not an exception.
    status, numbers = txn._paginated_relationship_numbers("owner/repo", 999999, "blockedBy", "gh")
    assert status == "issue_not_found"
    assert numbers == set()

    # Callers built on top of it must degrade to a safe False/empty result, not raise.
    assert txn._target_blockedby_contains("owner/repo", 999999, 1, "gh") is False
    assert txn._readback_dependencies("owner/repo", 999999, [1], "gh") is False
    assert txn._readback_blocking("owner/repo", 999999, [1], "gh") is False

    current_status, current_numbers = txn._current_relationship_numbers(
        "owner/repo", 999999, "blocked_by", "gh"
    )
    assert current_status == "issue_not_found"
    assert current_numbers == set()


def test_apply_relationship_direction_reports_actual_state_unavailable_on_current_state_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the current-state readback (used to compute the mutation diff) returns
    issue_not_found/invalid_payload, _apply_relationship_direction must not raise and
    must report failed_readback.actual_issue_numbers as the literal sentinel
    "actual_state_unavailable" (#1946 Owner P1-2), not a fabricated empty list.
    """
    monkeypatch.setattr(
        txn, "_current_relationship_numbers", lambda *_a, **_k: ("issue_not_found", set())
    )

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no mutation should be attempted when current-state is unavailable")

    monkeypatch.setattr(txn, "_issue_register_dependency", _boom)
    monkeypatch.setattr(txn, "_issue_graphql_ids", _boom)

    result = txn._apply_relationship_direction(
        "owner/repo", 42, [10, 20], "blocked_by", "gh", sleep_fn=lambda _d: None
    )
    assert result.verified is False
    assert result.mutated_numbers == []
    assert result.failed_readback is not None
    assert result.failed_readback["actual_issue_numbers"] == "actual_state_unavailable"
    assert result.failed_readback["error_kind"] == "actual_state_unavailable"


# ---------------------------------------------------------------------------
# #1946 Owner P1-1 (required tests 6/7): live_canary_blocking_direction.sh's
# _cleanup() function, driven via `bash -c` (no new repo-tracked test file --
# stays within this file's Allowed Paths). Sources the canary script with
# LIVE_CANARY_TEST_MODE=1 (skips preflight/steps/trap registration; see the
# guard added to that script), stubs `gh` with a controllable fake function,
# and asserts on _cleanup's exit code / whether it discovered+removed a
# leftover relationship.
# ---------------------------------------------------------------------------


def _run_cleanup_scenario(gh_fake_body: str, state_lines: str) -> tuple[int, str]:
    """Run live_canary_blocking_direction.sh's _cleanup() in isolation via bash -c.

    ``gh_fake_body`` is the body of a ``gh() { ... }`` shell function that stands in for
    the real ``gh`` CLI (no live GitHub calls). ``state_lines`` sets up
    DISPOSABLE_A_NUMBER/DISPOSABLE_B_NUMBER/RELATIONSHIP_REGISTERED/CLEANUP_FAILED/REPO/
    LOG_FILE before invoking ``_cleanup`` in a subshell.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    script_lines = [
        "set -u",
        "export LIVE_CANARY_TEST_MODE=1",
        f'source "{_LIVE_CANARY_SCRIPT}"',
        state_lines,
        "gh() {",
        gh_fake_body,
        "}",
        "export -f gh",
        "( _cleanup )",
        'echo "EXITCODE=$?"',
    ]
    script = "\n".join(script_lines)
    cp = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return cp.returncode, cp.stdout + cp.stderr


def test_live_canary_cleanup_exits_nonzero_when_relationship_removal_fails() -> None:
    """Required test 6: relationship-removal mutation failure -> _cleanup exits 1, not 0.

    Regression: the previous _cleanup only set CLEANUP_FAILED but never changed the exit
    code, so the trap-driven exit stayed whatever the main script last set (often 0).
    """
    gh_fake_body = (
        '  local args=("$@")\n'
        '  if [[ "${args[*]}" == *"removeBlockedBy"* ]]; then\n'
        "    return 1\n"
        "  fi\n"
        '  if [[ "${args[*]}" == *"blocking(first:10)"* ]]; then\n'
        '    echo "200"\n'
        "    return 0\n"
        "  fi\n"
        "  return 0\n"
    )
    state_lines = "\n".join(
        [
            'REPO="owner/repo"',
            'LOG_FILE="$(mktemp)"',
            'DISPOSABLE_A_NUMBER="100"',
            'DISPOSABLE_B_NUMBER="200"',
            'RELATIONSHIP_REGISTERED="unknown"',
            'CLEANUP_FAILED="0"',
        ]
    )
    _rc, out = _run_cleanup_scenario(gh_fake_body, state_lines)
    assert "EXITCODE=1" in out, f"expected _cleanup to exit 1 on removal failure; got: {out!r}"


def test_live_canary_cleanup_exits_nonzero_when_issue_close_fails() -> None:
    """Required test 6 (variant): issue-close failure alone -> _cleanup exits 1."""
    gh_fake_body = (
        '  local args=("$@")\n'
        '  if [[ "${args[*]}" == *"issue"*"close"* ]]; then\n'
        "    return 1\n"
        "  fi\n"
        "  return 0\n"
    )
    state_lines = "\n".join(
        [
            'REPO="owner/repo"',
            'LOG_FILE="$(mktemp)"',
            'DISPOSABLE_A_NUMBER="101"',
            'DISPOSABLE_B_NUMBER="201"',
            'RELATIONSHIP_REGISTERED="yes"',
            'CLEANUP_FAILED="0"',
        ]
    )
    _rc, out = _run_cleanup_scenario(gh_fake_body, state_lines)
    assert "EXITCODE=1" in out, f"expected _cleanup to exit 1 on issue-close failure; got: {out!r}"


def test_live_canary_cleanup_discovers_and_removes_leftover_relationship_after_partial_failure() -> None:
    """Required test 7: even when RELATIONSHIP_REGISTERED is left "unknown" (as happens
    after a create_issue_txn.py partial_failure -- the caller does not know whether the
    mutation actually landed), _cleanup independently reads back the current relationship
    state and removes it if present, rather than skipping cleanup entirely.
    """
    marker_flag = "REMOVED_MARKER_SEEN"
    gh_fake_body = (
        '  local args=("$@")\n'
        '  if [[ "${args[*]}" == *"removeBlockedBy"* ]]; then\n'
        f'    echo "{marker_flag}" >> "${{LOG_FILE}}.marker"\n'
        "    return 0\n"
        "  fi\n"
        '  if [[ "${args[*]}" == *"issue"*"close"* ]]; then\n'
        "    return 0\n"
        "  fi\n"
        '  if [[ "${args[*]}" == *"blocking(first:10)"* ]]; then\n'
        '    if [ ! -f "${LOG_FILE}.marker" ]; then\n'
        '      echo "202"\n'
        "    fi\n"
        "    return 0\n"
        "  fi\n"
        '  if [[ "${args[*]}" == *"{issue(number:\\$number){id}}"* ]]; then\n'
        '    echo "NODE_ID_STUB"\n'
        "    return 0\n"
        "  fi\n"
        "  return 0\n"
    )
    state_lines = "\n".join(
        [
            'REPO="owner/repo"',
            'LOG_FILE="$(mktemp)"',
            'DISPOSABLE_A_NUMBER="102"',
            'DISPOSABLE_B_NUMBER="202"',
            'RELATIONSHIP_REGISTERED="unknown"',
            'CLEANUP_FAILED="0"',
        ]
    )
    _rc, out = _run_cleanup_scenario(gh_fake_body, state_lines)
    assert "EXITCODE=0" in out, (
        f"expected _cleanup to exit 0 after successfully removing the leftover link; got: {out!r}"
    )
    assert marker_flag in out or "cleanup: relationship readback confirms" in out, (
        f"expected _cleanup to have discovered and removed the leftover relationship; got: {out!r}"
    )
