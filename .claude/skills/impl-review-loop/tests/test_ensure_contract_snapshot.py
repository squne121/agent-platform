"""
tests/test_ensure_contract_snapshot.py

Unit tests for ensure_contract_snapshot.py

AC5: ensure_contract_snapshot.py の unit test が PASS する
AC8: CONTRACT_REVIEW_RESULT_V1.status に human_judgment を投稿しないことを unit test で固定する
AC9: issue body が materialization 中に更新された場合、投稿せず exit 50 になる（unit test で確認）
AC10: GitHub comment 投稿は idempotency marker により重複投稿されない（unit test で確認）
AC11: 403/429/422 から盲目的に retry しない（unit test で確認）

B3 invariant: status: ok implies contract_snapshot_url is not None (unit test で確認)
B4 schema: post_status key (not post_result)
B2 atomicity: body_sha256 OR updatedAt 変化 → stale_or_conflicting_snapshot
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import builtins
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import ensure_contract_snapshot from worktree path
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_ECS_PATH = _SCRIPTS_DIR / "ensure_contract_snapshot.py"

spec = importlib.util.spec_from_file_location("ensure_contract_snapshot", _ECS_PATH)
assert spec is not None and spec.loader is not None
_ecs_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_ecs_mod)  # type: ignore[union-attr]

ensure_contract_snapshot = _ecs_mod.ensure_contract_snapshot
classify_post_http_error = _ecs_mod.classify_post_http_error
find_idempotency_marker = _ecs_mod.find_idempotency_marker
sha256_of = _ecs_mod.sha256_of
compute_comments_digest = _ecs_mod.compute_comments_digest
is_go_fresh = _ecs_mod.is_go_fresh
is_go_current = _ecs_mod.is_go_current
is_go_base_binding_current = _ecs_mod.is_go_base_binding_current
_real_verify_snapshot_authority_postcondition = (
    _ecs_mod.verify_snapshot_authority_postcondition
)
_real_verify_controlled_publisher_comment_id_binding = (
    _ecs_mod.verify_controlled_publisher_comment_id_binding
)
# Issue #2635: captured before any autouse fixture stubs
# ``_ecs_mod.patch_comment`` to a success-only lambda, so a focused test can
# restore the real POST/PATCH/GET orchestration end-to-end.
_real_patch_comment = _ecs_mod.patch_comment
# Issue #3004: captured before the autouse fixture stubs it, so the
# post-publish race regressions can run the real base ref/sha capture against
# a fake ``gh`` remote.
_real_capture_base_ref_and_sha = _ecs_mod.capture_base_ref_and_sha

_PARSER_PATH = (
    _HERE.parent.parent / "issue-contract-review" / "scripts"
    / "contract_review_result_parser.py"
)
_parser_spec = importlib.util.spec_from_file_location(
    "contract_review_result_parser_for_snapshot_tests", _PARSER_PATH
)
assert _parser_spec is not None and _parser_spec.loader is not None
_parser_mod = importlib.util.module_from_spec(_parser_spec)
_parser_spec.loader.exec_module(_parser_mod)  # type: ignore[union-attr]

# Use post_status constants (B4)
POST_STATUS_POSTED = _ecs_mod.POST_STATUS_POSTED
POST_STATUS_DEDUPED = _ecs_mod.POST_STATUS_DEDUPED
POST_STATUS_DRY_RUN = _ecs_mod.POST_STATUS_DRY_RUN
POST_STATUS_PERMISSION_DENIED = _ecs_mod.POST_STATUS_PERMISSION_DENIED
POST_STATUS_RATE_LIMITED = _ecs_mod.POST_STATUS_RATE_LIMITED
POST_STATUS_VALIDATION_FAILED = _ecs_mod.POST_STATUS_VALIDATION_FAILED
POST_STATUS_AMBIGUOUS = _ecs_mod.POST_STATUS_AMBIGUOUS
POST_STATUS_NOT_REQUESTED = _ecs_mod.POST_STATUS_NOT_REQUESTED

# Legacy aliases preserved for backward compat (still exported from module)
POST_RESULT_POSTED = POST_STATUS_POSTED
POST_RESULT_DEDUPED = POST_STATUS_DEDUPED
POST_RESULT_DRY_RUN = POST_STATUS_DRY_RUN
POST_RESULT_PERMISSION_DENIED = POST_STATUS_PERMISSION_DENIED
POST_RESULT_RATE_LIMITED = POST_STATUS_RATE_LIMITED
POST_RESULT_VALIDATION_FAILED = POST_STATUS_VALIDATION_FAILED
POST_RESULT_AMBIGUOUS = POST_STATUS_AMBIGUOUS
POST_RESULT_NOT_REQUESTED = POST_STATUS_NOT_REQUESTED

# ---------------------------------------------------------------------------
# #1475: default the controlled-publisher comment id binding check to success
# for all tests in this file. AC5 regression scenarios in this file predate
# the binding check and do not exercise real `gh api` subprocess calls; tests
# that specifically exercise the binding check live in
# test_contract_snapshot_author_binding.py and override this fixture locally.
#
# #1537: also default the new two-phase fingerprint materialize steps
# (base_ref/base_sha capture, PATCH) to success for the same reason -- these
# pre-existing regression scenarios exercise the POST flow / precedence
# rules, not the fingerprint mechanism itself. Tests that specifically
# exercise fingerprint materialization override these locally.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_trusted_comment_id_binding(monkeypatch):
    monkeypatch.setattr(
        _ecs_mod,
        "verify_controlled_publisher_comment_id_binding",
        lambda *args, **kwargs: (True, None),
    )
    monkeypatch.setattr(
        _ecs_mod,
        "capture_base_ref_and_sha",
        lambda *args, **kwargs: ("main", "a" * 40),
    )
    monkeypatch.setattr(
        _ecs_mod,
        "patch_comment",
        lambda *args, **kwargs: (True, None),
    )
    monkeypatch.setattr(
        _ecs_mod,
        "verify_snapshot_authority_postcondition",
        lambda *args, **kwargs: (True, None),
    )


# ---------------------------------------------------------------------------
# Helpers for mocking
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = 817
_REPO = "squne121/agent-platform"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

_SAMPLE_BODY = "## Test Issue Body\nSome content here.\n\n## Allowed Paths\n- tracked.txt\n"
_SAMPLE_BODY_SHA256 = sha256_of(_SAMPLE_BODY)
_SAMPLE_UPDATED_AT = "2026-06-13T08:00:00Z"


def test_contract_review_materializer_uses_bounded_outer_deadline_only():
    """The publisher deadline must not change run_contract_review_once CLI semantics."""
    subprocess_result = MagicMock(stdout='{"status": "go"}')
    with patch.object(_ecs_mod.subprocess, "run", return_value=subprocess_result) as run:
        result, error = _ecs_mod.run_contract_review_once(_ISSUE_NUMBER, _REPO)

    assert error is None
    assert result == {"status": "go"}
    command = run.call_args.args[0]
    assert run.call_args.kwargs["timeout"] == 360
    assert "--timeout" not in command

_GO_COMMENT = {
    "id": 1001,
    "html_url": f"{_ISSUE_URL}#issuecomment-1001",
    "created_at": "2026-06-13T08:00:00Z",
    "updated_at": "2026-06-13T08:00:00Z",
    "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-13T08:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
  body_sha256: "{_SAMPLE_BODY_SHA256}"
```
""",
}

_BLOCKED_COMMENT = {
    "id": 1002,
    "html_url": f"{_ISSUE_URL}#issuecomment-1002",
    "created_at": "2026-06-13T09:00:00Z",
    "updated_at": "2026-06-13T09:00:00Z",
    "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-06-13T09:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
```
""",
}


def _mock_parser_mod(
    comments: list[dict],
    go_comment: dict | None = None,
    latest: dict | None = None,
    fetch_err: str | None = None,
) -> MagicMock:
    """Create a mock parser module."""
    mod = MagicMock()
    mod.fetch_issue_comments.return_value = ([] if fetch_err else comments, fetch_err)

    # Build results from comments
    results = []
    if go_comment:
        results.append({
            "comment_id": go_comment["id"],
            "html_url": go_comment["html_url"],
            "created_at": go_comment["created_at"],
            "status": "go",
            "inner": _fresh_inner(
                _SAMPLE_BODY_SHA256, fingerprint_comment_id=go_comment["id"]
            ),
        })

    mod.parse_contract_review_results.return_value = results
    mod.find_latest_go.return_value = results[0] if results and results[0]["status"] == "go" else None
    mod.find_latest_authoritative_go.return_value = (
        results[0] if results and results[0]["status"] == "go" else None
    )
    mod.find_latest_result.return_value = latest or (results[0] if results else None)
    return mod


def _fresh_inner(
    body_sha256: str,
    *,
    fingerprint_comment_id: int | None = 1001,
    base_ref: str = "main",
    base_sha: str = "a" * 40,
) -> dict:
    inner = {
        "body_sha256": body_sha256,
        "checks": {
            "product_spec_check": {
                "schema": "product_spec_check/v1",
                "applicability": "applicable",
                "decision": "pass",
                "triggers": {},
                "conditions": {},
                "blocked_reasons": [],
                "body_sha256": body_sha256,
                "source_provenance": {
                    "source_type": "github_issue_body",
                    "body_file": None,
                },
            },
            "vc_preflight": {"classifications": []},
        },
    }
    if fingerprint_comment_id is not None:
        inner["expected_contract_fingerprint"] = {
            "issue_number": _ISSUE_NUMBER,
            "contract_source_kind": "issue_comment",
            "contract_source_id": str(fingerprint_comment_id),
            "contract_body_sha256": body_sha256,
            "allowed_paths_normalized_sha256": "b" * 64,
            "base_ref": base_ref,
            "base_sha_at_snapshot": base_sha,
        }
    return inner


def _make_review_result(status: str) -> dict:
    checks = {
        "readiness": "go" if status == "go" else "needs_fix",
        "blockers": "pass" if status == "go" else None,
        "product_spec": "pass" if status == "go" else None,
        "product_spec_check": _fresh_inner(_SAMPLE_BODY_SHA256)["checks"][
            "product_spec_check"
        ] if status == "go" else None,
        "vc_preflight": "pass" if status == "go" else None,
    }
    return {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": status,
        "readiness_status": "go" if status == "go" else "needs_fix",
        "checks": checks,
        "vc_preflight_classifications": [{"ac": "AC1", "decision": "pass"}],
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Basic tests
# ---------------------------------------------------------------------------


class TestCheckOnlyMode:
    """check-only mode tests."""

    def test_existing_go_matches_current_body_returns_ok(self, monkeypatch):
        """Existing go comment → status: ok, source: existing_go."""
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": "2026-06-13T08:00:00Z"
            },
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert result["contract_snapshot_url"] == _GO_COMMENT["html_url"]
        # B3: status: ok implies contract_snapshot_url is not None
        assert result["contract_snapshot_url"] is not None

    def test_no_go_returns_human_judgment(self, monkeypatch):
        """No go comment in check-only mode → human_judgment (not blocked)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"

    def test_no_candidate_at_all_still_returns_no_existing_go_comment(self, monkeypatch):
        """#1513 AC4: with zero parsed results (no candidate of any kind),
        the reason code stays no_existing_go_comment -- not the new
        go_like_candidate_authority_unmet classification (non-regression)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "human_judgment"
        assert any("no_existing_go_comment" in e for e in result["errors"])
        assert not any("go_like_candidate_authority_unmet" in e for e in result["errors"])

    def test_go_like_candidate_excluded_by_authority_returns_new_reason_code(self, monkeypatch):
        """#1513 AC3: a go-status comment is present in the parsed results
        (find_latest_result() detects a candidate) but is excluded from
        find_latest_go(trusted_only=True, fingerprint_ready_only=True)
        candidacy (untrusted publisher or not fingerprint-ready) -> the new
        go_like_candidate_authority_unmet reason code, distinct from
        no_existing_go_comment."""
        go_like_result = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": "2026-06-13T08:00:00Z",
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = MagicMock()
        parser_mod.fetch_issue_comments.return_value = ([_GO_COMMENT], None)
        parser_mod.parse_contract_review_results.return_value = [go_like_result]
        # trusted_only=True excludes the untrusted-author go comment from
        # both latest-result precedence and go candidacy.
        parser_mod.find_latest_result.return_value = None
        parser_mod.find_latest_go.return_value = None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"
        assert any("go_like_candidate_authority_unmet" in e for e in result["errors"])
        assert not any(
            e.startswith("no_existing_go_comment") for e in result["errors"]
        )

    def test_latest_blocked_returns_blocked_needs_refinement(self, monkeypatch):
        """Latest result is blocked → blocked_needs_refinement."""
        latest = {
            "comment_id": 1002,
            "html_url": _BLOCKED_COMMENT["html_url"],
            "status": "blocked",
            "created_at": "2026-06-13T09:00:00Z",
        }
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT, _BLOCKED_COMMENT],
            go_comment=None,
            latest=latest,
        )
        parser_mod.find_latest_result.return_value = latest
        parser_mod.find_latest_go.return_value = None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "blocked_needs_refinement"
        assert result["source"] == "latest_blocked"


class TestFreshGoSnapshots:
    """#1445: existing go is usable only for the current canonical body hash."""

    def test_current_head_runs_producer_even_when_existing_go_is_fresh(self):
        """GIVEN fresh GO WHEN current-head is requested THEN producer evidence is refreshed."""
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": _GO_COMMENT["created_at"],
            },
        )
        full_envelope = {
            "schema": "baseline_vc_preflight/v1",
            "status": "pass",
            "generated_at": "2026-07-12T00:00:00Z",
            "errors": [],
            "source": {"body_sha256": "sha256:" + "c" * 64},
            "results": [],
            "evidence_mode": "current-head",
            "head_sha": "a" * 40,
            "reviewed_head_sha": "a" * 40,
            "head_after_sha": "a" * 40,
            "clean_before": True,
            "clean_after": True,
            "fallback_detected": False,
            "human_review_required": False,
            "stop_condition_triggered": False,
        }
        review_result = _make_review_result("go")
        review_result["vc_evidence"] = full_envelope
        review_result["current_vc_result"] = full_envelope

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(review_result, None),
                ) as run_once:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="auto",
                        evidence_mode="current-head",
                        cwd="/tmp/pr-worktree",
                        reviewed_head_sha="a" * 40,
                    )

        assert run_once.call_count == 1
        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert result["contract_snapshot_url"] == _GO_COMMENT["html_url"]
        assert result["vc_evidence"] == full_envelope
        assert result["current_vc_result"] == full_envelope


    @pytest.mark.parametrize(
        "body_sha256",
        [
            None,
            "",
            1,
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            " sha256:" + "a" * 64,
            sha256_of("different body"),
        ],
    )
    def test_invalid_or_stale_go_never_returns_existing_go(self, body_sha256):
        stale_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": body_sha256},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [stale_go]
        parser_mod.find_latest_go.return_value = stale_go
        parser_mod.find_latest_result.return_value = stale_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"

    def test_stale_go_auto_without_post_materializes_dry_run(self):
        stale_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": sha256_of("different body")},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [stale_go]
        parser_mod.find_latest_go.return_value = stale_go
        parser_mod.find_latest_result.return_value = stale_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ) as review:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=False
                    )

        assert result["status"] == "dry_run_would_post"
        review.assert_called_once()

    def test_stale_go_at_post_check_does_not_dedupe(self):
        stale_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": sha256_of("different body")},
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [stale_go]]
        parser_mod.find_latest_result.side_effect = [None, None]
        parser_mod.find_latest_go.side_effect = [None, stale_go]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ):
                    with patch.object(
                        _ecs_mod,
                        "post_comment",
                        return_value=(
                            "https://example.test/comment",
                            POST_STATUS_POSTED,
                            201,
                        ),
                    ) as post:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=True
                        )

        assert result["source"] == "materialized_go"
        post.assert_called_once()

    def test_fresh_go_at_post_check_dedupes(self):
        fresh_go = {
            "comment_id": 1002,
            "html_url": "https://example.test/fresh",
            "created_at": "2026-06-13T09:00:00Z",
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [fresh_go]]
        parser_mod.find_latest_result.side_effect = [None, fresh_go]
        parser_mod.find_latest_go.side_effect = [None, fresh_go]
        fresh_comment = {**_GO_COMMENT, "id": 1002, "html_url": fresh_go["html_url"]}
        parser_mod.fetch_issue_comments.side_effect = [([], None), ([fresh_comment], None)]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(_make_review_result("go"), None)):
                    with patch.object(_ecs_mod, "post_comment") as post:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=True
                        )

        assert result["source"] == "existing_go"
        assert result["post_status"] == POST_STATUS_DEDUPED
        post.assert_not_called()

    def test_updated_at_only_change_with_fresh_go_dedupes(self):
        fresh_go = {
            "comment_id": 1002,
            "html_url": "https://example.test/fresh",
            "created_at": "2026-06-13T09:00:00Z",
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [fresh_go]]
        parser_mod.find_latest_result.side_effect = [None, fresh_go]
        parser_mod.find_latest_go.side_effect = [None, fresh_go]
        fresh_comment = {**_GO_COMMENT, "id": 1002, "html_url": fresh_go["html_url"]}
        parser_mod.fetch_issue_comments.side_effect = [([], None), ([fresh_comment], None)]
        snapshots = [
            (_SAMPLE_BODY, "one", None),
            (_SAMPLE_BODY, "two", None),
            (_SAMPLE_BODY, "two", None),
        ]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=snapshots):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(_make_review_result("go"), None)
                ):
                    with patch.object(_ecs_mod, "post_comment") as post:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=True
                        )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert result["post_status"] == POST_STATUS_DEDUPED
        post.assert_not_called()

    def test_existing_go_snapshot_change_retries_then_conflicts(self):
        fresh_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [fresh_go]
        parser_mod.find_latest_result.return_value = fresh_go
        parser_mod.find_latest_go.return_value = fresh_go
        snapshots = [
            (_SAMPLE_BODY, "one", None),
            (_SAMPLE_BODY + " changed", "two", None),
            (_SAMPLE_BODY, "three", None),
            (_SAMPLE_BODY + " changed again", "four", None),
        ]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=snapshots):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                )

        assert result["status"] == "stale_or_conflicting_snapshot"

    def test_existing_go_updated_at_change_retries_then_succeeds(self):
        fresh_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [fresh_go]
        parser_mod.find_latest_result.return_value = fresh_go
        parser_mod.find_latest_go.return_value = fresh_go
        snapshots = [
            (_SAMPLE_BODY, "one", None),
            (_SAMPLE_BODY, "two", None),
            (_SAMPLE_BODY, "two", None),
            (_SAMPLE_BODY, "two", None),
        ]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=snapshots):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"

    def test_hashless_go_auto_materializes_dry_run(self):
        legacy_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [legacy_go]
        parser_mod.find_latest_result.return_value = legacy_go
        parser_mod.find_latest_go.return_value = legacy_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ) as review:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=False
                    )

        assert result["status"] == "dry_run_would_post"
        review.assert_called_once()

    def test_fresh_go_without_classifications_materializes_dry_run(self):
        incomplete_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": _SAMPLE_BODY_SHA256},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [incomplete_go]
        parser_mod.find_latest_result.return_value = incomplete_go
        parser_mod.find_latest_go.return_value = incomplete_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ) as review:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=False
                    )

        assert result["status"] == "dry_run_would_post"
        review.assert_called_once()

    def test_current_body_legacy_go_without_product_spec_payload_materializes_dry_run(self):
        legacy_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {
                "body_sha256": _SAMPLE_BODY_SHA256,
                "checks": {"vc_preflight": {"classifications": []}},
            },
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [legacy_go]
        parser_mod.find_latest_go.return_value = legacy_go
        parser_mod.find_latest_result.return_value = legacy_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ) as review:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=False
                    )

        assert result["status"] == "dry_run_would_post"
        review.assert_called_once()

    def test_parser_result_with_canonical_body_sha256_is_consumed(self):
        parser_mod = _ecs_mod._import_parser_module()
        results = parser_mod.parse_contract_review_results([_GO_COMMENT], _ISSUE_URL)
        go_result = parser_mod.find_latest_go(results)

        assert go_result is not None
        assert is_go_fresh(go_result, _SAMPLE_BODY_SHA256)
        assert not is_go_current(go_result, _SAMPLE_BODY_SHA256)


class TestVcPreflightClassificationsCompleteness:
    """Consumer acceptance of parsed baseline VC classifications is fail-closed."""

    @staticmethod
    def _go_result(vc_preflight: object) -> dict:
        inner = _fresh_inner(_SAMPLE_BODY_SHA256)
        inner["checks"]["vc_preflight"] = vc_preflight
        return {"status": "go", "inner": inner}

    def test_given_raw_classifications_scalar_and_pass_decision_when_current_then_rejects(self):
        raw_classifications = json.dumps(
            {"ac": "AC10", "stdout_head": ["x" * 16_190]}
        )
        go_result = self._go_result(
            {
                "decision": "pass",
                "classifications": raw_classifications,
            }
        )

        assert not is_go_current(go_result, _SAMPLE_BODY_SHA256)

    @pytest.mark.parametrize(
        ("vc_preflight", "case"),
        [
            ({"classifications": "raw"}, "decision_missing"),
            ({"decision": "fail", "classifications": "raw"}, "decision_non_pass"),
            ({"decision": "pass", "classifications": {"ac": "AC1"}}, "mapping"),
            ({"decision": "pass", "classifications": 1}, "integer"),
            ({"decision": "pass", "classifications": None}, "null"),
        ],
    )
    def test_given_non_list_classifications_without_explicit_pass_when_current_then_rejects(
        self, vc_preflight: object, case: str
    ):
        assert case
        assert not is_go_current(self._go_result(vc_preflight), _SAMPLE_BODY_SHA256)


class TestContractReviewComment:
    """Materialized snapshots retain VC baseline classifications for consumers."""

    def test_comment_serializes_vc_preflight_classifications(self):
        fingerprint = _ecs_mod.compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="1",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=["tracked.txt"],
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        body = _ecs_mod._build_contract_review_comment(
            issue_number=_ISSUE_NUMBER,
            repo=_REPO,
            review_result=_make_review_result("go"),
            idempotency_marker="<!-- marker -->",
            body_sha256=_SAMPLE_BODY_SHA256,
            expected_contract_fingerprint=fingerprint,
        )
        parser_mod = _ecs_mod._import_parser_module()
        results = parser_mod.parse_contract_review_results(
            [{"id": 1, "html_url": "https://example.test/1", "created_at": "now", "body": body}],
            _ISSUE_URL,
        )
        go_result = parser_mod.find_latest_go(results)

        assert go_result is not None
        assert is_go_current(go_result, _SAMPLE_BODY_SHA256)
        assert go_result["inner"]["checks"]["vc_preflight"]["classifications"] == [
            {"ac": "AC1", "decision": "pass"}
        ]
        assert go_result["inner"]["checks"]["product_spec_check"]["schema"] == "product_spec_check/v1"


# ---------------------------------------------------------------------------
# AC8: human_judgment は投稿しない
# ---------------------------------------------------------------------------


class TestHumanJudgmentNotPosted:
    """AC8: CONTRACT_REVIEW_RESULT_V1.status に human_judgment を投稿しない。"""

    def test_human_judgment_status_not_posted(self, monkeypatch):
        """
        run_contract_review_once returns human_judgment → no GitHub comment posted,
        ensure_contract_snapshot returns status: human_judgment (not blocked, not posted).
        """
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        review_result = _make_review_result("human_judgment")

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        # Must NOT post a comment when human_judgment
        assert len(posted) == 0, "must not post comment for human_judgment"
        assert result["status"] == "human_judgment"
        assert result["post_status"] != POST_STATUS_POSTED  # B4: key is post_status

    def test_human_judgment_source_correct(self, monkeypatch):
        """human_judgment source is correctly set."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        review_result = _make_review_result("human_judgment")

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="auto",
                        do_post=False,
                    )

        assert result["status"] == "human_judgment"
        assert result["source"] == "human_judgment"


# ---------------------------------------------------------------------------
# AC9: stale body → exit 50 (stale_or_conflicting_snapshot)
# ---------------------------------------------------------------------------


class TestStaleBodyDetection:
    """AC9: issue body が materialization 中に更新された場合、投稿せず exit 50 になる。"""

    def test_body_changed_between_check_and_post_returns_exit50(self, monkeypatch):
        """
        Body sha256 changes between initial fetch and pre-post re-fetch → stale_or_conflicting_snapshot.
        """
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        # Simulate body changing between initial fetch and re-fetch
        body_calls = [0]

        def fetch_snapshot_side_effect(issue_number, repo, timeout=30):
            body_calls[0] += 1
            if body_calls[0] == 1:
                return (_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)
            else:
                # Body changed on second call
                return (_SAMPLE_BODY + "\n\nBODY CHANGED", _SAMPLE_UPDATED_AT, None)

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=fetch_snapshot_side_effect):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "stale_or_conflicting_snapshot", (
            f"Expected stale_or_conflicting_snapshot, got {result['status']}"
        )
        assert len(posted) == 0, "must not post when body changed (stale)"
        assert result["body_sha256_at_check"] != result["body_sha256_at_post"]

    def test_updated_at_changed_returns_exit50(self, monkeypatch):
        """
        B2: updatedAt changes between initial fetch and pre-post re-fetch → stale_or_conflicting_snapshot.
        """
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        body_calls = [0]

        def fetch_snapshot_side_effect(issue_number, repo, timeout=30):
            body_calls[0] += 1
            if body_calls[0] == 1:
                return (_SAMPLE_BODY, "2026-06-13T08:00:00Z", None)
            else:
                # updatedAt changed (e.g. someone edited the issue)
                return (_SAMPLE_BODY, "2026-06-13T09:30:00Z", None)

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=fetch_snapshot_side_effect):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "stale_or_conflicting_snapshot", (
            f"B2: updatedAt mismatch must produce stale_or_conflicting_snapshot, got {result['status']}"
        )
        assert len(posted) == 0, "must not post when updatedAt changed"

    def test_body_unchanged_proceeds_to_post(self, monkeypatch):
        """Body sha256 unchanged → proceed to post (no stale)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "ok"
        assert result["source"] == "materialized_go"
        assert len(posted) == 1
        # B3: status: ok implies contract_snapshot_url is not None
        assert result["contract_snapshot_url"] is not None


# ---------------------------------------------------------------------------
# AC10: idempotency marker で重複投稿しない
# ---------------------------------------------------------------------------


class TestIdempotencyMarker:
    """AC10: GitHub comment 投稿は idempotency marker により重複投稿されない。"""

    def test_marker_only_comment_does_not_authorize_existing_go(self, monkeypatch):
        """
        A marker without a parser-valid fresh go result is not snapshot authority.
        """
        # Build a comment that has the idempotency marker
        idempotency_marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=_ISSUE_NUMBER,
            body_sha256=_SAMPLE_BODY_SHA256,
        )
        marker_comment = {
            "id": 2001,
            "html_url": f"{_ISSUE_URL}#issuecomment-2001",
            "created_at": "2026-06-13T10:00:00Z",
            "updated_at": "2026-06-13T10:00:00Z",
            "body": f"{idempotency_marker}\n\nSome contract review content.",
        }

        # Parser returns no valid go (marker comment is not CONTRACT_REVIEW_RESULT_V1)
        parser_mod = _mock_parser_mod(
            comments=[marker_comment],
            go_comment=None,
            latest=None,
        )
        parser_mod.fetch_issue_comments.return_value = ([marker_comment], None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert len(posted) == 1, "marker-only comment must not suppress materialization"
        assert result["post_status"] == POST_STATUS_POSTED  # B4: post_status
        assert result["idempotency_marker_found"] is False
        assert result["status"] == "ok"
        # B3: status: ok implies contract_snapshot_url is not None
        assert result["contract_snapshot_url"] is not None

    def test_idempotency_marker_not_found_allows_post(self, monkeypatch):
        """If no idempotency marker, posting proceeds normally."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert len(posted) == 1, "should post when no idempotency marker"
        assert result["post_status"] == POST_STATUS_POSTED  # B4: post_status
        assert result["idempotency_marker_found"] is False

    def test_idempotency_marker_correct_format(self):
        """Verify idempotency marker format includes required fields."""
        marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=_ISSUE_NUMBER,
            body_sha256=_SAMPLE_BODY_SHA256,
        )
        assert f"issue={_ISSUE_NUMBER}" in marker
        assert _SAMPLE_BODY_SHA256 in marker
        assert "schema=CONTRACT_REVIEW_RESULT_V1" in marker
        assert marker.startswith("<!--")
        assert marker.endswith("-->")


# ---------------------------------------------------------------------------
# AC11: 403/429/422 から盲目的に retry しない
# ---------------------------------------------------------------------------


class TestRateLimitAndPermissionErrors:
    """AC11: 403/429/422 から盲目的に retry しない。"""

    @pytest.mark.parametrize(
        "http_status,expected_code",
        [
            (403, POST_STATUS_PERMISSION_DENIED),
            (429, POST_STATUS_RATE_LIMITED),
            (422, POST_STATUS_VALIDATION_FAILED),
        ],
    )
    def test_http_error_classified_no_retry(self, http_status, expected_code, monkeypatch):
        """403/429/422 → specific error code, no retry attempted."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        call_count = [0]

        def fake_post(issue_number, repo, body, timeout=30):
            call_count[0] += 1
            code = classify_post_http_error(http_status)
            return (None, code, http_status)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        # Only ONE attempt — no blind retry
        assert call_count[0] == 1, f"Expected 1 attempt, got {call_count[0]} (blind retry forbidden)"
        assert result["post_status"] == expected_code  # B4: post_status
        assert result["http_status"] == http_status
        # Status should be human_judgment for 403/429, etc.
        assert result["status"] == "human_judgment"

    def test_classify_http_error_403(self):
        assert classify_post_http_error(403) == POST_STATUS_PERMISSION_DENIED

    def test_classify_http_error_429(self):
        assert classify_post_http_error(429) == POST_STATUS_RATE_LIMITED

    def test_classify_http_error_422(self):
        assert classify_post_http_error(422) == POST_STATUS_VALIDATION_FAILED

    def test_classify_http_error_unknown(self):
        assert classify_post_http_error(500) == POST_STATUS_AMBIGUOUS

    def test_classify_http_error_503(self):
        assert classify_post_http_error(503) == POST_STATUS_AMBIGUOUS

    def test_rate_limit_no_retry_429(self, monkeypatch):
        """429 rate limit → no retry, status: human_judgment."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        call_count = [0]

        def fake_post(issue_number, repo, body, timeout=30):
            call_count[0] += 1
            return (None, POST_STATUS_RATE_LIMITED, 429)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert call_count[0] == 1, "no retry on 429"
        assert result["status"] == "human_judgment"
        assert result["post_status"] == POST_STATUS_RATE_LIMITED  # B4

    def test_permission_denied_no_retry_403(self, monkeypatch):
        """403 permission denied → no retry, status: human_judgment."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        call_count = [0]

        def fake_post(issue_number, repo, body, timeout=30):
            call_count[0] += 1
            return (None, POST_STATUS_PERMISSION_DENIED, 403)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert call_count[0] == 1, "no retry on 403"
        assert result["status"] == "human_judgment"
        assert result["post_status"] == POST_STATUS_PERMISSION_DENIED  # B4


# ---------------------------------------------------------------------------
# B3: status: ok implies contract_snapshot_url is not None
# ---------------------------------------------------------------------------


class TestOkImpliesSnapshotUrl:
    """B3: status: ok must imply contract_snapshot_url is not None."""

    def test_ok_status_has_contract_snapshot_url(self, monkeypatch):
        """Whenever status: ok is returned, contract_snapshot_url must be non-null."""
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": "2026-06-13T08:00:00Z"
            },
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        if result["status"] == "ok":
            assert result["contract_snapshot_url"] is not None, (
                "B3 violation: status: ok must imply contract_snapshot_url is not None"
            )

    def test_posted_ok_has_contract_snapshot_url(self, monkeypatch):
        """After successful post, status: ok with non-null contract_snapshot_url."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "ok"
        assert result["contract_snapshot_url"] is not None, (
            "B3: status: ok implies contract_snapshot_url must be non-null"
        )


# ---------------------------------------------------------------------------
# Dry-run mode tests (B3: dry-run → status: dry_run_would_post, NOT ok)
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """dry-run mode does not post and does NOT return status: ok (B3)."""

    def test_dry_run_no_post(self, monkeypatch):
        """dry-run mode: no GitHub mutation, status: dry_run_would_post (not ok)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="dry-run",
                            do_post=True,  # even with --post, dry-run wins
                        )

        assert len(posted) == 0
        assert result["post_status"] == POST_STATUS_DRY_RUN  # B4: post_status
        # B3: dry-run must NOT return status: ok (contract_snapshot_url is None)
        assert result["status"] == "dry_run_would_post", (
            f"B3: dry-run must return dry_run_would_post, not {result['status']}"
        )
        assert result["contract_snapshot_url"] is None

    def test_auto_no_post_flag(self, monkeypatch):
        """auto mode without --post: no GitHub mutation, status: dry_run_would_post."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=False,
                        )

        assert len(posted) == 0
        assert result["post_status"] == POST_STATUS_DRY_RUN  # B4: post_status
        # B3: no-post must NOT return status: ok
        assert result["status"] == "dry_run_would_post", (
            f"B3: no-post must return dry_run_would_post, not {result['status']}"
        )
        assert result["contract_snapshot_url"] is None


# ---------------------------------------------------------------------------
# Runtime error propagation
# ---------------------------------------------------------------------------


class TestRuntimeErrorPropagation:
    """runtime_error from run_contract_review_once → propagated."""

    def test_run_contract_review_once_error_propagated(self, monkeypatch):
        """If run_contract_review_once fails, status: runtime_error."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(None, "json_parse_error:test")
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="auto",
                    )

        assert result["status"] == "runtime_error"
        assert any("json_parse_error" in e for e in result["errors"])

    def test_body_fetch_error_returns_runtime_error(self, monkeypatch):
        """Body fetch error → runtime_error."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(None, None, "gh_timeout")):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "runtime_error"


# ---------------------------------------------------------------------------
# idempotency marker utility tests
# ---------------------------------------------------------------------------


class TestIdempotencyMarkerUtility:
    """Unit tests for find_idempotency_marker helper."""

    def test_finds_matching_marker(self):
        marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=100,
            body_sha256="sha256:abc123",
        )
        comments = [
            {"id": 1, "html_url": "https://example.com/1", "body": f"Some content\n{marker}\nMore"},
        ]
        result = find_idempotency_marker(comments, 100, "sha256:abc123")
        assert result == "https://example.com/1"

    def test_no_match_returns_none(self):
        comments = [
            {"id": 1, "html_url": "https://example.com/1", "body": "No marker here"},
        ]
        result = find_idempotency_marker(comments, 100, "sha256:abc123")
        assert result is None

    def test_different_sha_no_match(self):
        marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=100,
            body_sha256="sha256:oldvalue",
        )
        comments = [
            {"id": 1, "html_url": "https://example.com/1", "body": marker},
        ]
        result = find_idempotency_marker(comments, 100, "sha256:newvalue")
        assert result is None


# ---------------------------------------------------------------------------
# B2: issue_updated_at fields present in result
# ---------------------------------------------------------------------------


class TestAtomicityFields:
    """B2: body_sha256_at_check, issue_updated_at_at_check, comments_digest_at_check present."""

    def test_atomicity_fields_populated(self, monkeypatch):
        """B2 fields are in result after initial snapshot fetch."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert "body_sha256_at_check" in result
        assert "issue_updated_at_at_check" in result
        assert "comments_digest_at_check" in result
        assert result["body_sha256_at_check"] == _SAMPLE_BODY_SHA256
        assert result["issue_updated_at_at_check"] == _SAMPLE_UPDATED_AT


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint (Issue #1537)
# ---------------------------------------------------------------------------

extract_allowed_paths_from_body = _ecs_mod.extract_allowed_paths_from_body
compute_expected_contract_fingerprint = _ecs_mod.compute_expected_contract_fingerprint
capture_base_ref_and_sha = _ecs_mod.capture_base_ref_and_sha
patch_comment = _ecs_mod.patch_comment

_real_parser_mod_for_fp_tests = _ecs_mod._import_parser_module()
parse_contract_review_results = _real_parser_mod_for_fp_tests.parse_contract_review_results
find_latest_go = _real_parser_mod_for_fp_tests.find_latest_go
is_fingerprint_ready_go = _real_parser_mod_for_fp_tests.is_fingerprint_ready_go

_BODY_WITH_ALLOWED_PATHS = """## Outcome

Something.

## Allowed Paths

- `.claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py`
- `.claude/skills/impl-review-loop/tests/test_ensure_contract_snapshot.py`

## Stop Conditions

- N/A
"""


class TestExtractAllowedPathsFromBody:
    def test_extracts_bullet_paths(self):
        paths = extract_allowed_paths_from_body(_BODY_WITH_ALLOWED_PATHS)
        assert paths == [
            ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py",
            ".claude/skills/impl-review-loop/tests/test_ensure_contract_snapshot.py",
        ]

    def test_missing_section_returns_empty(self):
        assert extract_allowed_paths_from_body("## Outcome\n\nNo allowed paths here.\n") == []

    def test_empty_body_returns_empty(self):
        assert extract_allowed_paths_from_body("") == []


class TestComputeExpectedContractFingerprint:
    def test_returns_seven_keys(self):
        fp = compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="12345",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=[".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"],
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        assert set(fp.keys()) == {
            "issue_number",
            "contract_source_kind",
            "contract_source_id",
            "contract_body_sha256",
            "allowed_paths_normalized_sha256",
            "base_ref",
            "base_sha_at_snapshot",
        }
        assert fp["issue_number"] == _ISSUE_NUMBER
        assert fp["contract_source_kind"] == "issue_comment"
        assert fp["contract_source_id"] == "12345"
        assert fp["contract_body_sha256"] == _SAMPLE_BODY_SHA256
        assert fp["base_ref"] == "main"
        assert fp["base_sha_at_snapshot"] == "a" * 40

    def test_allowed_paths_hash_matches_gate_recomputation(self):
        """AC4: the gate must be able to recompute an identical
        allowed_paths_normalized_sha256 from the same Allowed Paths list."""
        import sys as _sys

        gate_scripts_dir = (
            Path(__file__).resolve().parents[4]
            / ".claude"
            / "skills"
            / "pr-review-judge"
            / "scripts"
        )
        if str(gate_scripts_dir) not in _sys.path:
            _sys.path.insert(0, str(gate_scripts_dir))
        from allowed_paths_review_gate import AllowedPathsGateEvaluator  # noqa: E402

        allowed_paths = ["src/**", "docs/foo.md"]
        fp = compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="1",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=allowed_paths,
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        evaluator = AllowedPathsGateEvaluator(
            pr_number=1,
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
            current_base_sha="x",
            diff_base_sha="x",
            head_sha="y",
            reviewed_head_sha="y",
            allowed_paths=allowed_paths,
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            contract_source_kind="issue_comment",
            contract_source_id="1",
            expected_contract_fingerprint=None,
            issue_number=_ISSUE_NUMBER,
        )
        assert fp["allowed_paths_normalized_sha256"] == evaluator.compute_allowed_paths_hash()

    def test_no_sha256_prefix_on_allowed_paths_hash(self):
        fp = compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="1",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=["src/**"],
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        assert not fp["allowed_paths_normalized_sha256"].startswith("sha256:")


class TestCaptureBaseRefAndSha:
    def test_success_returns_both_values(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {
                                "name": "main",
                                "target": {"oid": "a" * 40},
                            }
                        }
                    }
                }
            )
            return result

        with patch.object(_ecs_mod.subprocess, "run", side_effect=fake_run):
            base_ref, base_sha = capture_base_ref_and_sha(_REPO)

        assert base_ref == "main"
        assert base_sha == "a" * 40

    def test_uses_one_graphql_readback_for_branch_and_tip(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "defaultBranchRef": {
                            "name": "main",
                            "target": {"oid": "b" * 40},
                        }
                    }
                }
            }
        )
        with patch.object(_ecs_mod.subprocess, "run", return_value=result) as run:
            assert capture_base_ref_and_sha(_REPO) == ("main", "b" * 40)

        run.assert_called_once()
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["gh", "api", "graphql"]
        assert "defaultBranchRef" in next(arg for arg in cmd if arg.startswith("query="))

    def test_default_branch_failure_returns_none_none(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            return result

        with patch.object(_ecs_mod.subprocess, "run", side_effect=fake_run):
            base_ref, base_sha = capture_base_ref_and_sha(_REPO)

        assert base_ref is None
        assert base_sha is None

    def test_graphql_payload_without_tip_fails_closed(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(
            {"data": {"repository": {"defaultBranchRef": {"name": "main", "target": {}}}}}
        )

        with patch.object(_ecs_mod.subprocess, "run", return_value=result):
            base_ref, base_sha = capture_base_ref_and_sha(_REPO)

        assert base_ref is None
        assert base_sha is None


class TestFingerprintMaterializeEndToEnd:
    """Integration coverage for the two-phase POST+PATCH materialize flow
    (Issue #1537 AC1/AC5): the final PATCHed comment body must embed a
    fingerprint whose contract_source_id equals the real posted comment id.
    """

    def test_post_then_patch_embeds_fingerprint_bound_to_real_comment_id(self, monkeypatch):
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        real_comment_id = 555444
        real_url = f"{_ISSUE_URL}#issuecomment-{real_comment_id}"

        def fake_post(issue_number, repo, body, timeout=30):
            return (real_url, POST_STATUS_POSTED, None)

        patched_calls = []

        def fake_patch(issue_number, repo, comment_id, body, **kwargs):
            patched_calls.append((comment_id, body))
            return (True, None)

        monkeypatch.setattr(_ecs_mod, "capture_base_ref_and_sha", lambda *a, **kw: ("main", "d" * 40))
        monkeypatch.setattr(_ecs_mod, "patch_comment", fake_patch)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "ok"
        assert result["source"] == "materialized_go"
        assert len(patched_calls) == 1
        patched_comment_id, patched_body = patched_calls[0]
        assert patched_comment_id == real_comment_id

        parsed = parse_contract_review_results(
            [{"id": real_comment_id, "html_url": real_url, "created_at": "now", "body": patched_body}],
            _ISSUE_URL,
        )
        go_entry = find_latest_go(parsed)
        assert go_entry is not None
        fp = go_entry["inner"]["expected_contract_fingerprint"]
        assert fp["contract_source_id"] == str(real_comment_id)
        assert fp["issue_number"] == _ISSUE_NUMBER
        assert fp["base_ref"] == "main"
        assert fp["base_sha_at_snapshot"] == "d" * 40
        assert is_fingerprint_ready_go(go_entry["inner"], real_comment_id, _ISSUE_NUMBER) is True

    def test_base_ref_capture_failure_blocks_materialization(self, monkeypatch):
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        monkeypatch.setattr(_ecs_mod, "capture_base_ref_and_sha", lambda *a, **kw: (None, None))

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-1", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "runtime_error"
        assert not posted, "must not post a go when base_ref/base_sha capture fails"

    def test_patch_failure_does_not_report_ok(self, monkeypatch):
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        monkeypatch.setattr(_ecs_mod, "capture_base_ref_and_sha", lambda *a, **kw: ("main", "e" * 40))
        monkeypatch.setattr(_ecs_mod, "patch_comment", lambda *a, **kw: (False, "patch_error"))

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_ISSUE_URL}#issuecomment-2", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "controlled_publisher_binding_failed"
        assert result["contract_snapshot_url"] is None

    def test_final_patch_internal_get_reconciliation_body_hash_mismatch_surfaces_bounded_diagnostics(
        self, monkeypatch
    ):
        """#1513 P2 fix_delta regression.

        When patch_comment()'s own internal GET reconciliation (the GET it
        performs right after the final fingerprint PATCH) observes a
        binding_body_hash_mismatch on the final PATCHed body, the bounded
        byte-level diagnostics it computes must reach
        ensure_contract_snapshot()'s result["errors"] -- not be dropped at
        patch_comment()'s own (bool, reason_code) return.

        This exercises the real patch_comment() and real
        verify_controlled_publisher_comment_id_binding() production code
        path end-to-end (both restored from the file's autouse
        return-value-only mock), not a mocked return value.
        """
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None
        # Wire the trust check through to the real parser implementation so
        # this end-to-end run exercises real publisher-trust logic too, not
        # just a MagicMock truthy default.
        parser_mod.is_trusted_snapshot_author = _parser_mod.is_trusted_snapshot_author

        review_result = _make_review_result("go")
        real_comment_id = 555777
        real_url = f"{_ISSUE_URL}#issuecomment-{real_comment_id}"
        tampered_final_body = (
            "tampered persisted body observed by patch_comment's internal GET"
        )
        endpoint = f"repos/{_REPO}/issues/comments/{real_comment_id}"

        staged_bodies: list[str] = []

        def fake_post(issue_number, repo, body, timeout=30):
            staged_bodies.append(body)
            return (real_url, POST_STATUS_POSTED, None)

        call_log = {"get": 0, "patch": 0}

        def fake_run(command, *, input=None, **kwargs):
            trusted_user = {"login": "squne121", "id": 63350259, "type": "User"}
            if "--method" in command:
                assert command[command.index("--method") + 1] == "PATCH"
                call_log["patch"] += 1
                return MagicMock(returncode=0, stdout=b"", stderr=b"")
            assert command[-1] == endpoint
            call_log["get"] += 1
            if call_log["get"] == 1:
                # Staging binding GET, invoked directly inside
                # ensure_contract_snapshot right after post_comment()
                # succeeds: must exactly match the just-staged body so the
                # flow proceeds to the fingerprint PATCH step below.
                body = staged_bodies[0]
            else:
                # patch_comment()'s own internal GET reconciliation after
                # the PATCH: deliberately diverges from the final PATCHed
                # body so its internal binding check observes a mismatch.
                body = tampered_final_body
            payload = {
                "id": real_comment_id,
                "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}",
                "html_url": f"{_ISSUE_URL}#issuecomment-{real_comment_id}",
                "user": trusted_user,
                "author_association": "OWNER",
                "body": body,
            }
            return MagicMock(
                returncode=0, stdout=json.dumps(payload).encode("utf-8"), stderr=b""
            )

        monkeypatch.setattr(
            _ecs_mod, "capture_base_ref_and_sha", lambda *a, **kw: ("main", "d" * 40)
        )
        # #1513 P2 fix_delta: explicitly restore the real functions the
        # file-wide autouse fixture mocks to a static (True, None), so this
        # regression actually walks the production patch_comment() ->
        # verify_controlled_publisher_comment_id_binding() code path.
        monkeypatch.setattr(_ecs_mod, "patch_comment", patch_comment)
        monkeypatch.setattr(
            _ecs_mod,
            "verify_controlled_publisher_comment_id_binding",
            _real_verify_controlled_publisher_comment_id_binding,
        )
        monkeypatch.setattr(_ecs_mod.subprocess, "run", fake_run)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "controlled_publisher_binding_failed"
        assert result["contract_snapshot_url"] is None
        assert call_log["patch"] == 1
        assert call_log["get"] == 2

        patch_failed_errors = [
            e for e in result["errors"] if e.startswith("fingerprint_patch_failed:")
        ]
        assert patch_failed_errors == [
            "fingerprint_patch_failed: patch_get_reconciliation_failed:binding_body_hash_mismatch"
        ]

        diagnostics_errors = [
            e
            for e in result["errors"]
            if e.startswith("binding_body_hash_mismatch_diagnostics:")
        ]
        assert len(diagnostics_errors) == 1
        diag = diagnostics_errors[0]
        # The final PATCHed body text itself is constructed internally by
        # _build_contract_review_comment and not directly captured by this
        # test's doubles, so assert on the fields whose values are
        # independently known to this test (the tampered actual body, and
        # the presence of every bounded diagnostics field) instead.
        assert "expected_byte_len=" in diag
        assert "first_diff_byte_offset=" in diag
        assert "expected_sha=" in diag
        assert f"actual_byte_len={len(tampered_final_body.encode('utf-8'))}" in diag
        assert f"actual_sha={sha256_of(tampered_final_body)}" in diag


class TestExistingGoFingerprintReuseGate:
    """Issue #1537: an existing go lacking a fingerprint must not be reused
    as current -- it must fall through to (re-)materialization."""

    def test_existing_go_without_fingerprint_is_not_reused_in_check_only(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": "2026-06-13T08:00:00Z",
            },
        )
        # Make this candidate legacy-like so real fingerprint-ready logic is
        # exercised rather than the mock's default return value.
        parser_mod.parse_contract_review_results.return_value[0]["inner"].pop(
            "expected_contract_fingerprint"
        )
        real_parser_mod = _ecs_mod._import_parser_module()
        parser_mod.is_fingerprint_ready_go = real_parser_mod.is_fingerprint_ready_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"


class TestExistingGoBaseBindingFreshness:
    """#1635: reuse additionally requires the live GitHub base binding."""

    def test_matching_live_base_reuses_existing_go_without_mutation(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("main", "a" * 40),
                ) as capture:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert capture.call_count == 1

    def test_base_sha_drift_is_not_reused_in_check_only(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("main", "c" * 40),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "human_judgment"
        assert result["contract_snapshot_url"] is None
        assert "existing_go_base_binding_drift" in result["errors"][0]

    def test_base_ref_drift_is_not_reused_in_check_only(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("release", "a" * 40),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "human_judgment"
        assert result["contract_snapshot_url"] is None
        assert "existing_go_base_binding_drift" in result["errors"][0]

    def test_base_readback_failure_is_fail_closed_for_existing_go(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=(None, None),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "runtime_error"
        assert result["contract_snapshot_url"] is None
        assert "base_ref_or_base_sha_capture_failed" in result["errors"][0]

    def test_auto_post_materializes_new_go_after_base_sha_drift(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )
        review_result = _make_review_result("go")
        posted_comment_id = 2002
        posted_url = f"{_ISSUE_URL}#issuecomment-{posted_comment_id}"
        patched_bodies = []

        def fake_patch(_issue, _repo, comment_id, body, **kwargs):
            assert comment_id == posted_comment_id
            patched_bodies.append(body)
            return True, None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("main", "d" * 40),
                ):
                    with patch.object(
                        _ecs_mod,
                        "run_contract_review_once",
                        return_value=(review_result, None),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "post_comment",
                            return_value=(posted_url, POST_STATUS_POSTED, 201),
                        ) as post:
                            with patch.object(_ecs_mod, "patch_comment", side_effect=fake_patch):
                                result = ensure_contract_snapshot(
                                    issue_number=_ISSUE_NUMBER,
                                    repo=_REPO,
                                    mode="auto",
                                    do_post=True,
                                )

        assert result["status"] == "ok"
        assert result["source"] == "materialized_go"
        post.assert_called_once()
        assert '"base_sha_at_snapshot":"' + ("d" * 40) + '"' in patched_bodies[0]


class TestFinalAuthorityPostcondition:
    """P1: no path may expose an ``ok`` snapshot after authority drift."""

    def test_existing_go_timestamp_authority_drift_restarts_materialization(self):
        """ABA-shaped timestamp drift must not reuse existing_go."""
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT], go_comment=_GO_COMMENT)
        snapshots = [
            (_SAMPLE_BODY, "2026-06-13T08:00:00Z", None),
            (_SAMPLE_BODY, "2026-06-13T08:00:00Z", None),
            (_SAMPLE_BODY, "2026-06-13T09:00:00Z", None),
        ]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                side_effect=snapshots,
            ):
                with patch.object(
                    _ecs_mod,
                    "verify_snapshot_authority_postcondition",
                    wraps=_real_verify_snapshot_authority_postcondition,
                ):
                    with patch.object(
                        _ecs_mod,
                        "run_contract_review_once",
                        return_value=(_make_review_result("go"), None),
                    ) as review:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="dry-run",
                        )

        assert result["status"] == "dry_run_would_post"
        assert result["source"] == "materialized_go"
        assert result["contract_snapshot_url"] is None
        review.assert_called_once()

    def test_existing_go_timestamp_authority_drift_is_fail_closed_in_check_only(self):
        """check-only never converts timestamp drift into existing_go reuse."""
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT], go_comment=_GO_COMMENT)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "verify_snapshot_authority_postcondition",
                    return_value=(False, "authority_issue_updated_at_mismatch"),
                ):
                    with patch.object(_ecs_mod, "run_contract_review_once") as review:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="check-only",
                        )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"
        assert result["contract_snapshot_url"] is None
        review.assert_not_called()

    def test_existing_go_base_moves_during_final_readback_returns_stale(self):
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT], go_comment=_GO_COMMENT)
        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "verify_snapshot_authority_postcondition",
                    return_value=(False, "authority_base_binding_drift"),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                    )

        assert result["status"] == "stale_or_conflicting_snapshot"
        assert result["contract_snapshot_url"] is None

    def test_concurrent_go_dedupe_drift_returns_stale(self):
        fresh_go = {
            "comment_id": 1002,
            "html_url": "https://example.test/fresh",
            "created_at": "2026-06-13T09:00:00Z",
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256, fingerprint_comment_id=1002),
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [fresh_go]]
        parser_mod.find_latest_result.side_effect = [None, fresh_go]
        parser_mod.find_latest_go.side_effect = [None, fresh_go]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ):
                    with patch.object(
                        _ecs_mod,
                        "verify_snapshot_authority_postcondition",
                        return_value=(False, "authority_base_binding_drift"),
                    ):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "stale_or_conflicting_snapshot"
        assert result["contract_snapshot_url"] is None

    def test_materialized_comment_drift_after_patch_returns_stale(self):
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ):
                    with patch.object(
                        _ecs_mod,
                        "post_comment",
                        return_value=(
                            f"{_ISSUE_URL}#issuecomment-1003",
                            POST_STATUS_POSTED,
                            201,
                        ),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "verify_snapshot_authority_postcondition",
                            return_value=(False, "authority_comment_fingerprint_mismatch"),
                        ):
                            result = ensure_contract_snapshot(
                                issue_number=_ISSUE_NUMBER,
                                repo=_REPO,
                                mode="auto",
                                do_post=True,
                            )

        assert result["status"] == "stale_or_conflicting_snapshot"
        assert result["contract_snapshot_url"] is None


class TestAuthorityPostconditionReadback:
    def test_issue_updated_at_drift_is_rejected(self, monkeypatch):
        # Time-only difference: every other authority input holds, so the
        # shared verifier (evaluated last) must still reject on updatedAt.
        remote = _PostPublishRaceRemote(updated_at_for=lambda n: _RACE_U0)
        first = _run_ensure_against_race_remote(monkeypatch, remote)
        assert first["status"] == "ok", first
        cid = remote.own_comment_ids()[0]
        comment_body = remote.comments[cid]["body"]
        parsed = _real_parser_mod_for_fp_tests.parse_contract_review_results(
            [{**remote.comments[cid], "author": "squne121", "author_id": 63350259,
              "author_type": "User", "author_association": "OWNER"}],
            _ISSUE_URL,
        )
        fingerprint = parsed[0]["inner"]["expected_contract_fingerprint"]
        remote.updated_at_for = lambda n: _RACE_U1
        remote.arm()

        with patch.object(
            _ecs_mod,
            "_import_parser_module",
            return_value=_real_parser_mod_for_fp_tests,
        ):
            ok, reason = _real_verify_snapshot_authority_postcondition(
                issue_number=_ISSUE_NUMBER,
                repo=_REPO,
                expected_body_sha256=_SAMPLE_BODY_SHA256,
                expected_updated_at=_RACE_U0,
                expected_comment_id=cid,
                expected_comment_body_sha256=sha256_of(comment_body),
                expected_fingerprint=fingerprint,
            )

        assert ok is False
        assert reason == "authority_issue_updated_at_mismatch"

    def test_comment_body_drift_is_rejected_before_authority_return(self):
        parser_mod = MagicMock()
        parser_mod.fetch_issue_comments.return_value = (
            [{**_GO_COMMENT, "body": "tampered"}],
            None,
        )
        with patch.object(
            _ecs_mod,
            "fetch_issue_snapshot",
            return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
        ):
            with patch.object(
                _ecs_mod,
                "capture_base_ref_and_sha",
                return_value=("main", "a" * 40),
            ):
                with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
                    ok, reason = _real_verify_snapshot_authority_postcondition(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        expected_body_sha256=_SAMPLE_BODY_SHA256,
                        expected_updated_at=_SAMPLE_UPDATED_AT,
                        expected_comment_id=1001,
                        expected_comment_body_sha256=sha256_of(_GO_COMMENT["body"]),
                        expected_fingerprint=_fresh_inner(_SAMPLE_BODY_SHA256)[
                            "expected_contract_fingerprint"
                        ],
                    )

        assert ok is False
        assert reason == "authority_comment_body_sha256_mismatch"


# ---------------------------------------------------------------------------
# Migrated from test_contract_snapshot_author_binding.py (Issue #1537 PR #1548
# Blocker 2 remediation): that file was outside Issue #1537's Allowed Paths.
# Its regression coverage (controlled-publisher comment-id binding, AC3/AC4
# untrusted-author rejection across all snapshot consumers) is preserved here
# verbatim, with module-level identifiers renamed (_AB_ / _ab_ prefix) to avoid
# colliding with this file's own module globals (e.g. _ISSUE_NUMBER/_REPO).
# This file's autouse _default_trusted_comment_id_binding fixture (above)
# already defaults capture_base_ref_and_sha / verify_controlled_publisher_
# comment_id_binding / patch_comment to success, matching the behavior the
# migrated tests' local patch.object() overrides rely on.
# ---------------------------------------------------------------------------

# Captured before the autouse _default_trusted_comment_id_binding fixture
# (defined above in this file) monkeypatches this attribute to always
# succeed -- these migrated tests exercise the REAL implementation.
_ab_real_verify_controlled_publisher_comment_id_binding = (
    _ecs_mod.verify_controlled_publisher_comment_id_binding
)

_AB_HERE = Path(__file__).resolve().parent
_AB_SCRIPTS_DIR = _AB_HERE.parent / "scripts"
_AB_ICR_SCRIPTS_DIR = _AB_HERE.parents[1] / "issue-contract-review" / "scripts"


def _ab_load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_run_once_mod = _ab_load(
    "run_contract_review_once_binding", _AB_ICR_SCRIPTS_DIR / "run_contract_review_once.py"
)
_parser_mod = _ab_load(
    "contract_review_result_parser_binding",
    _AB_ICR_SCRIPTS_DIR / "contract_review_result_parser.py",
)

_AB_ISSUE_NUMBER = 1475
_AB_REPO = "squne121/agent-platform"
_AB_ISSUE_URL = f"https://github.com/{_AB_REPO}/issues/{_AB_ISSUE_NUMBER}"
_AB_SAMPLE_BODY = "## Test body for #1475 binding tests\n\n## Allowed Paths\n- tracked.txt\n"
_AB_SAMPLE_BODY_SHA256 = _ecs_mod.sha256_of(_AB_SAMPLE_BODY)
_AB_SAMPLE_UPDATED_AT = "2026-07-12T00:00:00Z"

# #1475 fix_delta P1 item 2: the only entry in TRUSTED_CONTRACT_PUBLISHERS.
_AB_TRUSTED_AUTHOR_ID = 63350259
_AB_TRUSTED_LOGIN = "squne121"
_AB_TRUSTED_TYPE = "User"
_AB_TRUSTED_ASSOCIATION = "OWNER"


def _ab_go_comment(
    author,
    author_association,
    comment_id: int = 5001,
    author_id=None,
    author_type=None,
) -> dict:
    fingerprint = _ecs_mod.compute_expected_contract_fingerprint(
        issue_number=_AB_ISSUE_NUMBER,
        contract_source_id=str(comment_id),
        contract_body_sha256=_AB_SAMPLE_BODY_SHA256,
        allowed_paths=["tracked.txt"],
        base_ref="main",
        base_sha_at_snapshot="a" * 40,
    )
    return {
        "id": comment_id,
        "html_url": f"{_AB_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": "2026-07-12T00:00:00Z",
        "updated_at": "2026-07-12T00:00:00Z",
        "author": author,
        "author_association": author_association,
        "author_id": author_id,
        "author_type": author_type,
        "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-07-12T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_AB_ISSUE_URL}
  body_sha256: "{_AB_SAMPLE_BODY_SHA256}"
  expected_contract_fingerprint: '{json.dumps(fingerprint, separators=(",", ":"))}'
```
""",
    }


def _ab_trusted_go_comment(comment_id: int = 5001) -> dict:
    """A go comment authored by the sole allowlisted TRUSTED_CONTRACT_PUBLISHERS entry."""
    return _ab_go_comment(
        author=_AB_TRUSTED_LOGIN,
        author_association=_AB_TRUSTED_ASSOCIATION,
        comment_id=comment_id,
        author_id=_AB_TRUSTED_AUTHOR_ID,
        author_type=_AB_TRUSTED_TYPE,
    )


def _ab_make_go_review_result() -> dict:
    return {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": "go",
        "readiness_status": "go",
        "checks": {
            "readiness": "go",
            "blockers": "pass",
            "product_spec": "pass",
            "product_spec_check": {
                "schema": "product_spec_check/v1",
                "applicability": "not_applicable",
                "decision": "pass",
                "triggers": {},
                "conditions": {},
                "blocked_reasons": [],
                "body_sha256": _AB_SAMPLE_BODY_SHA256,
                "source_provenance": {"source_type": "github_issue_body", "body_file": None},
            },
            "vc_preflight": "pass",
        },
        "vc_preflight_classifications": [],
        "errors": [],
    }


def _ab_mock_parser_mod_no_go() -> MagicMock:
    mod = MagicMock()
    mod.fetch_issue_comments.return_value = ([], None)
    mod.parse_contract_review_results.return_value = []
    mod.find_latest_go.return_value = None
    mod.find_latest_result.return_value = None
    return mod


# ---------------------------------------------------------------------------
# AC3: controlled publisher comment ID binding
# ---------------------------------------------------------------------------


def test_controlled_publisher_comment_id_binding_is_required():
    """AC3: controlled publisher の expected comment ID と remote readback
    comment ID が一致する場合だけ materialized snapshot を成功扱いし、
    不一致・欠落を fail-closed にすることを確認する。"""
    parser_mod = _ab_mock_parser_mod_no_go()
    review_result = _ab_make_go_review_result()

    def fake_post(issue_number, repo, body, timeout=30):
        return (f"{_AB_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

    # Mismatched binding → fail-closed, no status: ok, no contract_snapshot_url.
    # #1537: capture_base_ref_and_sha is a two-phase fingerprint materialize
    # step invoked before the binding verify this test exercises; default it
    # to success since this test's concern is the binding check, not
    # fingerprint materialization mechanics (mirrors the "Matching binding"
    # case below).
    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
        ):
            with patch.object(
                _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
            ):
                with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                    with patch.object(
                        _ecs_mod,
                        "verify_controlled_publisher_comment_id_binding",
                        return_value=(False, "binding_id_mismatch"),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "capture_base_ref_and_sha",
                            return_value=("main", "a" * 40),
                        ):
                            mismatched_result = _ecs_mod.ensure_contract_snapshot(
                                issue_number=_AB_ISSUE_NUMBER,
                                repo=_AB_REPO,
                                mode="auto",
                                do_post=True,
                            )

    assert mismatched_result["status"] == "controlled_publisher_binding_failed"
    assert mismatched_result["contract_snapshot_url"] is None

    # Missing expected_comment_id → fail-closed without any subprocess call.
    bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
        _AB_ISSUE_NUMBER, _AB_REPO, None
    )
    assert bound_ok is False
    assert reason == "missing_comment_id"

    # Matching binding → status: ok with a non-null contract_snapshot_url.
    # #1537: capture_base_ref_and_sha / patch_comment are the two-phase
    # fingerprint materialize steps added after the binding verify this test
    # exercises; default them to success since this test's concern is the
    # binding check, not fingerprint materialization mechanics.
    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
        ):
            with patch.object(
                _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
            ):
                with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                    with patch.object(
                        _ecs_mod,
                        "verify_controlled_publisher_comment_id_binding",
                        return_value=(True, None),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "capture_base_ref_and_sha",
                            return_value=("main", "a" * 40),
                        ):
                            with patch.object(
                                _ecs_mod, "patch_comment", return_value=(True, None)
                            ):
                                matched_result = _ecs_mod.ensure_contract_snapshot(
                                    issue_number=_AB_ISSUE_NUMBER,
                                    repo=_AB_REPO,
                                    mode="auto",
                                    do_post=True,
                                )

    assert matched_result["status"] == "ok"
    assert matched_result["contract_snapshot_url"] is not None


def test_all_snapshot_consumers_reject_untrusted_go():
    """AC4: run_contract_review_once.py / ensure_contract_snapshot.py /
    (build_intake_capsule は未移管) の各 snapshot 採用経路で、untrusted author が
    投稿した完全な schema-valid `status: go` を採用しないことを確認する。"""
    untrusted = _ab_go_comment(author="random-outsider", author_association="NONE")

    # 1. contract_review_result_parser.py (shared parser, both consumers use it)
    parsed = _parser_mod.parse_contract_review_results(
        [untrusted], expected_issue_url=_AB_ISSUE_URL
    )
    assert parsed[0]["is_trusted_author"] is False
    assert _parser_mod.find_latest_go(parsed, trusted_only=True) is None

    # 2. run_contract_review_once.py: check_existing_go_comment dedupe source
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[:2] == ["gh", "api"] and len(cmd) > 3 and "comments" in cmd[3]:
            result.returncode = 0
            result.stdout = json.dumps(untrusted) + "\n"
            result.stderr = ""
        else:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "not_needed_for_this_test"
        return result

    with patch("subprocess.run", side_effect=fake_run):
        go, _err = _run_once_mod.check_existing_go_comment(_AB_ISSUE_NUMBER, _AB_REPO)
    assert go is None

    # 3. ensure_contract_snapshot.py: check-only mode existing-go adoption
    parser_mod = MagicMock()
    parser_mod.fetch_issue_comments.return_value = ([untrusted], None)
    parser_mod.parse_contract_review_results.return_value = parsed
    parser_mod.find_latest_result.return_value = parsed[0]
    parser_mod.find_latest_go.side_effect = (
        lambda results, trusted_only=False, **_kwargs: (
            None
            if trusted_only
            else next((r for r in results if r.get("status") == "go"), None)
        )
    )

    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
        ):
            result = _ecs_mod.ensure_contract_snapshot(
                issue_number=_AB_ISSUE_NUMBER, repo=_AB_REPO, mode="check-only"
            )
    assert result["status"] != "ok"


class TestControlledPublisherCommentIdBinding:
    def test_extract_comment_id_from_url(self):
        assert _ecs_mod.extract_comment_id_from_url(f"{_AB_ISSUE_URL}#issuecomment-42") == 42
        assert _ecs_mod.extract_comment_id_from_url("https://example.test/no-anchor") is None
        assert _ecs_mod.extract_comment_id_from_url(None) is None
        assert _ecs_mod.extract_comment_id_from_url("") is None

    def test_binding_verification_missing_id_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, None
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    @staticmethod
    def _full_payload(
        comment_id=1234,
        issue_number=_AB_ISSUE_NUMBER,
        html_url=None,
        author_id=_AB_TRUSTED_AUTHOR_ID,
        author_login=_AB_TRUSTED_LOGIN,
        author_type=_AB_TRUSTED_TYPE,
        author_association=_AB_TRUSTED_ASSOCIATION,
        body="unused-body",
    ) -> dict:
        return {
            "id": comment_id,
            "issue_url": f"https://api.github.com/repos/{_AB_REPO}/issues/{issue_number}",
            "html_url": html_url or f"{_AB_ISSUE_URL}#issuecomment-{comment_id}",
            "user": {"id": author_id, "login": author_login, "type": author_type},
            "author_association": author_association,
            "body": body,
        }

    def test_binding_verification_id_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(self._full_payload(comment_id=999))
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_id_mismatch"

    def test_binding_verification_issue_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(self._full_payload(issue_number=9999))
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_issue_mismatch"

    def test_binding_verification_readback_error_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 1
            run_mock.return_value.stdout = ""
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_readback_error"

    def test_binding_verification_match_succeeds(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(self._full_payload())
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is True
        assert reason is None

    def test_binding_verification_html_url_mismatch_is_fail_closed(self):
        """fix_delta P1 item 3: the direct-GET html_url must match the
        comment id being verified, not just the numeric id field."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(html_url=f"{_AB_ISSUE_URL}#issuecomment-9999999")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_html_url_mismatch"

    def test_binding_verification_untrusted_collaborator_is_fail_closed(self):
        """fix_delta P1 item 2/3: an unauthorized COLLABORATOR posting a
        schema-valid, id/issue-matching comment must still be rejected."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(
                    author_id=987654,
                    author_login="some-collaborator",
                    author_type="User",
                    author_association="COLLABORATOR",
                )
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_untrusted_member_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(
                    author_id=555555,
                    author_login="some-member",
                    author_type="User",
                    author_association="MEMBER",
                )
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_correct_login_wrong_id_is_fail_closed(self):
        """The login string alone must never authorize -- only the id match does."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(author_id=1, author_login=_AB_TRUSTED_LOGIN)
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_correct_id_wrong_login_is_fail_closed(self):
        """A rename/spoofed-login on the correct id must never authorize."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(author_id=_AB_TRUSTED_AUTHOR_ID, author_login="not-squne121")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_type_mismatch_is_fail_closed(self):
        """A Bot account impersonating the trusted id/login must be rejected."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(author_type="Bot")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_body_hash_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(body="different-body-than-expected")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER,
                _AB_REPO,
                1234,
                expected_body_sha256=_ecs_mod.sha256_of("expected-body"),
            )
        assert bound_ok is False
        assert reason == "binding_body_hash_mismatch"

    def test_binding_verification_body_hash_mismatch_with_expected_body_text_populates_diagnostics(self):
        """#1513 AC1: when the caller supplies expected_body_text (holds the
        expected body in scope, not only its sha256), a
        binding_body_hash_mismatch populates diagnostics_out with bounded
        UTF-8 byte-level diagnostics."""
        expected_body = "expected-body"
        actual_body = "different-body-than-expected"
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(body=actual_body)
            )
            diagnostics: dict = {}
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER,
                _AB_REPO,
                1234,
                expected_body_sha256=_ecs_mod.sha256_of(expected_body),
                expected_body_text=expected_body,
                diagnostics_out=diagnostics,
            )
        assert bound_ok is False
        assert reason == "binding_body_hash_mismatch"
        assert diagnostics["expected_byte_len"] == len(expected_body.encode("utf-8"))
        assert diagnostics["actual_byte_len"] == len(actual_body.encode("utf-8"))
        assert diagnostics["first_diff_byte_offset"] == 0
        assert diagnostics["expected_sha"] == _ecs_mod.sha256_of(expected_body)
        assert diagnostics["actual_sha"] == _ecs_mod.sha256_of(actual_body)

    def test_binding_verification_body_hash_mismatch_without_expected_body_text_no_dummy_diagnostics(self):
        """#1513 AC2: when only expected_body_sha256 is passed (the caller
        does not hold the expected body text in scope), no dummy
        expected_byte_len / first_diff_byte_offset diagnostics are
        synthesized -- diagnostics_out stays empty."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(body="different-body-than-expected")
            )
            diagnostics: dict = {}
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER,
                _AB_REPO,
                1234,
                expected_body_sha256=_ecs_mod.sha256_of("expected-body"),
                diagnostics_out=diagnostics,
            )
        assert bound_ok is False
        assert reason == "binding_body_hash_mismatch"
        assert diagnostics == {}
        assert "expected_byte_len" not in diagnostics
        assert "first_diff_byte_offset" not in diagnostics

    def test_binding_verification_body_hash_match_succeeds(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(body="expected-body")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER,
                _AB_REPO,
                1234,
                expected_body_sha256=_ecs_mod.sha256_of("expected-body"),
            )
        assert bound_ok is True
        assert reason is None

    def test_binding_verification_comment_id_bool_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, True
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_binding_verification_comment_id_string_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, "1234"
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_binding_verification_comment_id_zero_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, 0
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_binding_verification_comment_id_negative_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, -1234
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_materialization_blocked_when_binding_fails(self):
        """GIVEN a successful comment post WHEN the id-binding readback
        mismatches THEN ensure_contract_snapshot fails closed and does not
        report status: ok, even though post_comment itself succeeded."""
        parser_mod = _ab_mock_parser_mod_no_go()
        review_result = _ab_make_go_review_result()

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_AB_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

        # #1537: capture_base_ref_and_sha is a two-phase fingerprint
        # materialize step invoked before the binding verify this test
        # exercises; default it to success since this test's concern is the
        # binding check, not fingerprint materialization mechanics.
        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod, "fetch_issue_snapshot",
                return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
                ):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        with patch.object(
                            _ecs_mod,
                            "verify_controlled_publisher_comment_id_binding",
                            return_value=(False, "binding_id_mismatch"),
                        ):
                            with patch.object(
                                _ecs_mod,
                                "capture_base_ref_and_sha",
                                return_value=("main", "a" * 40),
                            ):
                                result = _ecs_mod.ensure_contract_snapshot(
                                    issue_number=_AB_ISSUE_NUMBER,
                                    repo=_AB_REPO,
                                    mode="auto",
                                    do_post=True,
                                )

        assert result["status"] == "controlled_publisher_binding_failed"
        assert result["contract_snapshot_url"] is None
        assert any("binding" in e for e in result["errors"])

    def test_materialization_succeeds_when_binding_matches(self):
        parser_mod = _ab_mock_parser_mod_no_go()
        review_result = _ab_make_go_review_result()

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_AB_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

        # #1537: default the two-phase fingerprint materialize steps to
        # success; this test's concern is the binding check, not fingerprint
        # materialization mechanics.
        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod, "fetch_issue_snapshot",
                return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
                ):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        with patch.object(
                            _ecs_mod,
                            "verify_controlled_publisher_comment_id_binding",
                            return_value=(True, None),
                        ):
                            with patch.object(
                                _ecs_mod,
                                "capture_base_ref_and_sha",
                                return_value=("main", "a" * 40),
                            ):
                                with patch.object(
                                    _ecs_mod, "patch_comment", return_value=(True, None)
                                ):
                                    result = _ecs_mod.ensure_contract_snapshot(
                                        issue_number=_AB_ISSUE_NUMBER,
                                        repo=_AB_REPO,
                                        mode="auto",
                                        do_post=True,
                                    )

        assert result["status"] == "ok"
        assert result["contract_snapshot_url"] == f"{_AB_ISSUE_URL}#issuecomment-9999"


# ---------------------------------------------------------------------------
# AC4: all snapshot consumers reject untrusted go
# ---------------------------------------------------------------------------


class TestAllSnapshotConsumersRejectUntrustedGo:
    def test_contract_review_result_parser_marks_untrusted_go(self):
        untrusted = _ab_go_comment(author="random-outsider", author_association="NONE")
        results = _parser_mod.parse_contract_review_results(
            [untrusted], expected_issue_url=_AB_ISSUE_URL
        )
        assert results[0]["is_trusted_author"] is False
        assert _parser_mod.find_latest_go(results, trusted_only=True) is None
        assert _parser_mod.find_latest_go(results, trusted_only=False) is not None

    def test_run_contract_review_once_check_existing_go_rejects_untrusted(self):
        """GIVEN an untrusted, schema-valid status:go comment WHEN
        run_contract_review_once.check_existing_go_comment runs THEN the
        untrusted snapshot is not adopted as an existing go (dedupe source)."""
        untrusted = _ab_go_comment(author="random-outsider", author_association="NONE")

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:2] == ["gh", "api"] and len(cmd) > 3 and "comments" in cmd[3]:
                result.returncode = 0
                result.stdout = json.dumps(untrusted) + "\n"
                result.stderr = ""
            else:
                result.returncode = 1
                result.stdout = ""
                result.stderr = "not_needed_for_this_test"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            go, err = _run_once_mod.check_existing_go_comment(_AB_ISSUE_NUMBER, _AB_REPO)

        assert go is None

    def test_run_contract_review_once_check_existing_go_accepts_trusted(self):
        trusted = _ab_trusted_go_comment()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:2] == ["gh", "api"] and len(cmd) > 3 and "comments" in cmd[3]:
                result.returncode = 0
                result.stdout = json.dumps(trusted) + "\n"
                result.stderr = ""
            elif cmd[:3] == ["gh", "issue", "view"]:
                result.returncode = 0
                result.stdout = json.dumps({"body": _AB_SAMPLE_BODY})
                result.stderr = ""
            else:
                result.returncode = 1
                result.stdout = ""
                result.stderr = "not_needed_for_this_test"
            return result

        # is_go_current's fuller freshness contract (vc_preflight classifications,
        # product_spec_check body binding) is exercised in
        # test_ensure_contract_snapshot.py; here we isolate the trust filter by
        # stubbing that unrelated freshness predicate to True.
        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(_run_once_mod, "_is_current_go_snapshot", return_value=True):
                go, err = _run_once_mod.check_existing_go_comment(_AB_ISSUE_NUMBER, _AB_REPO)

        # Trusted + fresh body hash → adopted as an existing go.
        assert go is not None
        assert go["is_trusted_author"] is True

    def test_untrusted_blocked_does_not_preempt_trusted_go_precedence(self):
        """fix_delta P1 item 1 regression: a schema-valid untrusted `blocked`
        posted AFTER a trusted `go` must not take latest-result precedence in
        any of the three consumers."""
        trusted_go = _ab_trusted_go_comment(comment_id=1)
        trusted_go["created_at"] = "2026-07-12T00:00:00Z"
        untrusted_blocked = {
            "id": 2,
            "html_url": f"{_AB_ISSUE_URL}#issuecomment-2",
            "created_at": "2026-07-12T01:00:00Z",
            "author": "outside-actor",
            "author_association": "NONE",
            "author_id": 42424242,
            "author_type": "User",
            "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-07-12T01:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_AB_ISSUE_URL}
```
""",
        }
        comments = [trusted_go, untrusted_blocked]

        # 1. shared parser: trusted_only precedence must select the trusted go.
        parsed = _parser_mod.parse_contract_review_results(
            comments, expected_issue_url=_AB_ISSUE_URL
        )
        latest_trusted = _parser_mod.find_latest_result(parsed, trusted_only=True)
        assert latest_trusted is not None
        assert latest_trusted["status"] == "go"

    def test_untrusted_go_does_not_preempt_trusted_blocked_precedence(self):
        """The mirror case: an untrusted `go` posted after a trusted
        `blocked` must not be adopted as authoritative."""
        trusted_blocked = {
            "id": 1,
            "html_url": f"{_AB_ISSUE_URL}#issuecomment-1",
            "created_at": "2026-07-12T00:00:00Z",
            "author": _AB_TRUSTED_LOGIN,
            "author_association": _AB_TRUSTED_ASSOCIATION,
            "author_id": _AB_TRUSTED_AUTHOR_ID,
            "author_type": _AB_TRUSTED_TYPE,
            "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-07-12T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_AB_ISSUE_URL}
```
""",
        }
        untrusted_go = _ab_go_comment(
            author="outside-actor",
            author_association="NONE",
            comment_id=2,
            author_id=42424242,
            author_type="User",
        )
        untrusted_go["created_at"] = "2026-07-12T01:00:00Z"
        comments = [trusted_blocked, untrusted_go]

        parsed = _parser_mod.parse_contract_review_results(
            comments, expected_issue_url=_AB_ISSUE_URL
        )
        latest_trusted = _parser_mod.find_latest_result(parsed, trusted_only=True)
        assert latest_trusted is not None
        assert latest_trusted["status"] == "blocked"
        assert _parser_mod.find_latest_go(parsed, trusted_only=True) is None


class TestProducerParserEscapedAdvisoryRoundTrip:
    """Issue #2056: JSON-in-YAML must not expose JSON escapes to YAML."""

    def _comment_body(self) -> tuple[str, dict, list, dict]:
        fingerprint = {
            "issue_number": _ISSUE_NUMBER,
            "contract_source_kind": "issue_comment",
            "contract_source_id": "1001",
            "contract_body_sha256": _SAMPLE_BODY_SHA256,
            "allowed_paths_normalized_sha256": "b" * 64,
            "base_ref": "main",
            "base_sha_at_snapshot": "c" * 40,
        }
        classifications = [{
            "ac": "AC10",
            "stdout_head": [r"\^[[2K" + "x" * _parser_mod._MAX_JSON_FLOW_COLLECTION_CHARS],
        }]
        declared_path_overlap = {
            "advisory": True,
            "note": "y" * 2_771,
        }
        review_result = {
            "checks": {
                "readiness": "go",
                "blockers": "pass",
                "product_spec": "pass",
                "product_spec_check": {"decision": "pass"},
                "vc_preflight": "pass",
                "declared_path_overlap": declared_path_overlap,
            },
            "vc_preflight_classifications": classifications,
        }
        body = _ecs_mod._build_contract_review_comment(
            issue_number=_ISSUE_NUMBER,
            repo=_REPO,
            review_result=review_result,
            idempotency_marker="<!-- marker -->",
            body_sha256=_SAMPLE_BODY_SHA256,
            expected_contract_fingerprint=fingerprint,
        )
        return body, fingerprint, classifications, declared_path_overlap

    def test_given_large_escaped_advisory_when_produced_then_json_is_yaml_quoted(self):
        body, _fingerprint, _classifications, _overlap = self._comment_body()

        assert "      classifications: '" in body
        assert "    declared_path_overlap: '" in body
        assert "  expected_contract_fingerprint: '{" in body

    def test_given_oversized_classifications_when_produced_then_snapshot_uses_bounded_strict_list(self):
        body, _fingerprint, classifications, _overlap = self._comment_body()
        block = _parser_mod._extract_yaml_blocks(body)[0]

        parsed = _parser_mod._parse_simple_yaml_block(block)
        vc_preflight = parsed["CONTRACT_REVIEW_RESULT_V1"]["checks"]["vc_preflight"]
        assert isinstance(vc_preflight["classifications"], list)
        assert vc_preflight["classifications"][0]["ac"] == classifications[0]["ac"]
        assert "stdout_head" not in vc_preflight["classifications"][0]
        assert vc_preflight["classifications_transport"] == {
            "schema": "vc_preflight_classifications_transport/v1",
            "classification_count": 1,
            "original_payload_sha256": _ecs_mod.sha256_of(
                json.dumps(classifications, ensure_ascii=False, separators=(",", ":"))
            ),
            "compact": True,
            "truncated": True,
        }

    def test_given_oversized_classifications_when_fallback_parses_then_snapshot_keeps_strict_list(self, monkeypatch):
        body, _fingerprint, classifications, _overlap = self._comment_body()
        block = _parser_mod._extract_yaml_blocks(body)[0]
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        parsed = _parser_mod._parse_simple_yaml_block(block)
        vc_preflight = parsed["CONTRACT_REVIEW_RESULT_V1"]["checks"]["vc_preflight"]
        assert isinstance(vc_preflight["classifications"], list)
        assert vc_preflight["classifications"][0]["ac"] == classifications[0]["ac"]
        assert vc_preflight["classifications_transport"]["compact"] is True


class TestPatchCommentTransportAndReconciliation:
    """Issue #2619 real producer/PATCH/readback/parser regressions."""

    _COMMENT_ID = 2619001

    @staticmethod
    def _trusted_remote_comment(body: str) -> dict:
        return {
            "id": TestPatchCommentTransportAndReconciliation._COMMENT_ID,
            "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}",
            "html_url": f"{_ISSUE_URL}#issuecomment-{TestPatchCommentTransportAndReconciliation._COMMENT_ID}",
            "user": {"login": "squne121", "id": 63350259, "type": "User"},
            "author_association": "OWNER",
            "body": body,
        }

    @staticmethod
    def _review_result(classifications: list[dict]) -> dict:
        return {
            "checks": {
                "readiness": "go",
                "blockers": "pass",
                "product_spec": "pass",
                "product_spec_check": {"decision": "pass"},
                "vc_preflight": "pass",
                "declared_path_overlap": {"advisory": True},
            },
            "vc_preflight_classifications": classifications,
        }

    def _final_comment_body(self, classifications: list[dict]) -> str:
        fingerprint = {
            "issue_number": _ISSUE_NUMBER,
            "contract_source_kind": "issue_comment",
            "contract_source_id": str(self._COMMENT_ID),
            "contract_body_sha256": _SAMPLE_BODY_SHA256,
            "allowed_paths_normalized_sha256": "b" * 64,
            "base_ref": "main",
            "base_sha_at_snapshot": "c" * 40,
        }
        return _ecs_mod._build_contract_review_comment(
            issue_number=_ISSUE_NUMBER,
            repo=_REPO,
            review_result=self._review_result(classifications),
            idempotency_marker="<!-- marker -->",
            body_sha256=_SAMPLE_BODY_SHA256,
            expected_contract_fingerprint=fingerprint,
        )

    def _install_remote_boundary(self, monkeypatch, remote: dict, patch_response: str, after_patch=None):
        endpoint = f"repos/{_REPO}/issues/comments/{self._COMMENT_ID}"
        calls = {"patch": [], "get": []}

        def fake_run(command, *, input=None, **_kwargs):
            if "--method" in command:
                assert command[command.index("--method") + 1] == "PATCH"
                assert "--silent" in command
                assert command.count(endpoint) == 1
                assert command[command.index("--input") - 1] == endpoint
                assert input is not None
                payload = json.loads(input.decode("utf-8"))
                calls["patch"].append(payload)
                remote["body"] = payload["body"]
                if after_patch is not None:
                    after_patch(remote)
                if patch_response == "invalid_json":
                    return MagicMock(returncode=0, stdout=b"{")
                response = dict(remote)
                if patch_response == "id_mismatch":
                    response["id"] += 1
                elif patch_response == "body_mismatch":
                    response["body"] = "stale PATCH representation"
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps(response).encode("utf-8"),
                )

            assert command.count(endpoint) == 1
            assert command[-1] == endpoint
            calls["get"].append(command)
            if patch_response == "get_failure":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout=json.dumps(remote))

        monkeypatch.setattr(_ecs_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(_ecs_mod, "patch_comment", patch_comment)
        monkeypatch.setattr(
            _ecs_mod,
            "verify_controlled_publisher_comment_id_binding",
            _real_verify_controlled_publisher_comment_id_binding,
        )
        return calls

    def test_given_small_non_compacted_escaping_values_when_patched_then_exact_body_round_trips_through_real_parser(
        self, monkeypatch
    ):
        classifications = [
            {
                "ac": "AC1a",
                "decision": "pass",
                "stdout_head": [r"\^[[2K", "\x1b[2K", r"path\foo\bar"],
                "stderr_head": [r"\q", "runner's output", "line1\nline2\tvalue"],
            }
        ]
        final_body = self._final_comment_body(classifications)
        remote = self._trusted_remote_comment("provisional")
        calls = self._install_remote_boundary(monkeypatch, remote, "exact")

        ok, error = _ecs_mod.patch_comment(
            _ISSUE_NUMBER, _REPO, self._COMMENT_ID, final_body
        )

        assert (ok, error) == (True, None)
        assert len(calls["patch"]) == 1
        assert calls["patch"][0]["body"] == final_body
        assert remote["body"] == final_body
        assert len(calls["get"]) == 1
        parsed = _parser_mod.parse_contract_review_results([remote], _ISSUE_URL)
        assert len(parsed) == 1
        assert parsed[0]["status"] == "go"
        assert _parser_mod.is_fingerprint_ready_go(
            parsed[0]["inner"], self._COMMENT_ID, _ISSUE_NUMBER
        )
        parsed_classification = parsed[0]["inner"]["checks"]["vc_preflight"]["classifications"][0]
        # I-4-2 (#66): a real ESC in a verbose value cannot survive the `gh api`
        # readback, so it is carried as the literal text \x1b; the literal
        # ``\^[[2K`` stays distinct from it. Digest still binds the original.
        assert parsed_classification == {
            **classifications[0],
            "stdout_head": [r"\^[[2K", r"\x1b[2K", r"path\foo\bar"],
        }
        assert parsed[0]["inner"]["checks"]["vc_preflight"]["classifications_transport"] == {
            "schema": "vc_preflight_classifications_transport/v1",
            "classification_count": 1,
            "original_payload_sha256": _ecs_mod.sha256_of(
                json.dumps(classifications, ensure_ascii=False, separators=(",", ":"))
            ),
            "compact": False,
            "truncated": False,
        }

    def test_given_large_payload_when_patched_then_compaction_metadata_and_nonverbose_fields_round_trip_to_valid_go(
        self, monkeypatch
    ):
        classifications = [
            {
                "ac": "AC1b",
                "decision": "pass",
                "category": "regression_gate",
                "stdout_head": [r"\^[[2K" + "x" * 17_000],
                "stderr_head": ["\x1b[2K" + "y" * 17_000],
                "runner_env_delta": {"large": "z" * 17_000},
            }
        ]
        final_body = self._final_comment_body(classifications)
        remote = self._trusted_remote_comment("provisional")
        calls = self._install_remote_boundary(monkeypatch, remote, "exact")

        ok, error = _ecs_mod.patch_comment(
            _ISSUE_NUMBER, _REPO, self._COMMENT_ID, final_body
        )

        assert (ok, error) == (True, None)
        assert len(calls["patch"]) == 1
        assert calls["patch"][0]["body"] == final_body
        assert len(calls["get"]) == 1
        parsed = _parser_mod.parse_contract_review_results([remote], _ISSUE_URL)
        assert len(parsed) == 1
        assert parsed[0]["status"] == "go"
        vc_preflight = parsed[0]["inner"]["checks"]["vc_preflight"]
        assert vc_preflight["classifications"] == [{
            "ac": "AC1b", "decision": "pass", "category": "regression_gate"
        }]
        assert vc_preflight["classifications_transport"] == {
            "schema": "vc_preflight_classifications_transport/v1",
            "classification_count": 1,
            "original_payload_sha256": _ecs_mod.sha256_of(
                json.dumps(classifications, ensure_ascii=False, separators=(",", ":"))
            ),
            "compact": True,
            "truncated": True,
        }
        assert _parser_mod.is_fingerprint_ready_go(
            parsed[0]["inner"], self._COMMENT_ID, _ISSUE_NUMBER
        )

    @pytest.mark.parametrize(
        "patch_response", ["invalid_json", "id_mismatch", "body_mismatch"]
    )
    def test_given_untrusted_patch_success_representation_when_authoritative_get_fully_binds_then_reconciles_once(
        self, monkeypatch, patch_response
    ):
        remote = self._trusted_remote_comment("before")
        calls = self._install_remote_boundary(monkeypatch, remote, patch_response)

        ok, error = _ecs_mod.patch_comment(
            _ISSUE_NUMBER, _REPO, self._COMMENT_ID, "final body"
        )

        assert (ok, error) == (True, None)
        assert len(calls["patch"]) == 1
        assert len(calls["get"]) == 1

    @pytest.mark.parametrize(
        ("failure", "expected_error"),
        [
            ("get_failure", "binding_readback_error"),
            ("comment_id", "binding_id_mismatch"),
            ("body", "binding_body_hash_mismatch"),
            ("issue", "binding_issue_mismatch"),
            ("html_url", "binding_html_url_mismatch"),
            ("publisher", "binding_publisher_untrusted"),
        ],
    )
    def test_given_untrusted_patch_success_representation_when_authoritative_get_binding_fails_then_fails_closed(
        self, monkeypatch, failure, expected_error
    ):
        remote = self._trusted_remote_comment("before")

        def tamper_readback(comment):
            if failure == "comment_id":
                comment["id"] += 1
            elif failure == "body":
                comment["body"] = "wrong final body"
            elif failure == "issue":
                comment["issue_url"] = "https://api.github.com/repos/other/repo/issues/1"
            elif failure == "html_url":
                comment["html_url"] = f"{_ISSUE_URL}#issuecomment-999"
            elif failure == "publisher":
                comment["user"] = {"login": "outside", "id": 1, "type": "User"}

        calls = self._install_remote_boundary(
            monkeypatch,
            remote,
            "get_failure" if failure == "get_failure" else "body_mismatch",
            after_patch=tamper_readback,
        )

        ok, error = _ecs_mod.patch_comment(
            _ISSUE_NUMBER, _REPO, self._COMMENT_ID, "final body"
        )

        assert ok is False
        assert error == f"patch_get_reconciliation_failed:{expected_error}"
        assert len(calls["patch"]) == 1
        assert len(calls["get"]) == 1

    def test_body_hash_mismatch_at_internal_get_reconciliation_populates_diagnostics_when_expected_body_text_passed(
        self, monkeypatch
    ):
        """#1513 P2 fix_delta: patch_comment() must forward
        expected_body_text / diagnostics_out through to its internal GET
        reconciliation call (the PATCH-success branch) so a
        binding_body_hash_mismatch there also carries bounded byte-level
        diagnostics, not just the (False, reason_code) tuple."""
        remote = self._trusted_remote_comment("before")

        def tamper_readback(comment):
            comment["body"] = "wrong final body"

        calls = self._install_remote_boundary(
            monkeypatch, remote, "body_mismatch", after_patch=tamper_readback
        )

        final_body = "final body"
        diagnostics: dict = {}
        ok, error = _ecs_mod.patch_comment(
            _ISSUE_NUMBER,
            _REPO,
            self._COMMENT_ID,
            final_body,
            expected_body_text=final_body,
            diagnostics_out=diagnostics,
        )

        assert ok is False
        assert error == "patch_get_reconciliation_failed:binding_body_hash_mismatch"
        assert len(calls["patch"]) == 1
        assert len(calls["get"]) == 1
        assert diagnostics["expected_byte_len"] == len(final_body.encode("utf-8"))
        assert diagnostics["actual_byte_len"] == len("wrong final body".encode("utf-8"))
        assert diagnostics["expected_sha"] == _ecs_mod.sha256_of(final_body)
        assert diagnostics["actual_sha"] == _ecs_mod.sha256_of("wrong final body")
        assert "first_diff_byte_offset" in diagnostics

    @pytest.mark.parametrize(
        "patch_behavior", ["patch_success", "patch_nonzero", "patch_timeout"]
    )
    def test_all_three_internal_get_reconciliation_paths_forward_expected_body_text_and_diagnostics(
        self, monkeypatch, patch_behavior
    ):
        """#1513 P2 fix_delta (optional parametrize): all three
        patch_comment() internal GET reconciliation call sites -- after a
        successful PATCH subprocess exit, after a non-zero PATCH transport
        exit, and after a PATCH subprocess timeout -- must forward
        expected_body_text / diagnostics_out through to
        verify_controlled_publisher_comment_id_binding() so a
        binding_body_hash_mismatch observed on any of the three carries
        bounded diagnostics."""
        remote = self._trusted_remote_comment("wrong final body")
        endpoint = f"repos/{_REPO}/issues/comments/{self._COMMENT_ID}"

        def fake_run(command, *, input=None, **kwargs):
            if "--method" in command:
                if patch_behavior == "patch_success":
                    return MagicMock(returncode=0, stdout=b"", stderr=b"")
                if patch_behavior == "patch_nonzero":
                    return MagicMock(returncode=1, stdout=b"", stderr=b"boom")
                raise _ecs_mod.subprocess.TimeoutExpired(cmd=command, timeout=30)
            assert command[-1] == endpoint
            return MagicMock(
                returncode=0, stdout=json.dumps(remote).encode("utf-8"), stderr=b""
            )

        monkeypatch.setattr(_ecs_mod.subprocess, "run", fake_run)
        # #1513 P2 fix_delta: this test must exercise the real production
        # chain (patch_comment() -> verify_controlled_publisher_comment_id_
        # binding()); the file-wide autouse fixture otherwise mocks both to
        # a static (True, None).
        monkeypatch.setattr(
            _ecs_mod,
            "verify_controlled_publisher_comment_id_binding",
            _real_verify_controlled_publisher_comment_id_binding,
        )

        final_body = "final body"
        diagnostics: dict = {}
        ok, error = patch_comment(
            _ISSUE_NUMBER,
            _REPO,
            self._COMMENT_ID,
            final_body,
            expected_body_text=final_body,
            diagnostics_out=diagnostics,
        )

        assert ok is False
        if patch_behavior == "patch_success":
            assert error == "patch_get_reconciliation_failed:binding_body_hash_mismatch"
        elif patch_behavior == "patch_nonzero":
            assert error == "patch_transport_unreconciled"
        else:
            assert error == "patch_timeout_unreconciled"
        assert diagnostics["expected_byte_len"] == len(final_body.encode("utf-8"))
        assert diagnostics["actual_byte_len"] == len("wrong final body".encode("utf-8"))
        assert diagnostics["expected_sha"] == _ecs_mod.sha256_of(final_body)
        assert diagnostics["actual_sha"] == _ecs_mod.sha256_of("wrong final body")
        assert "first_diff_byte_offset" in diagnostics


class TestPostCommentTransportAndReconciliation:
    """Issue #2627 AC1/AC2/AC3: post_comment() transport unification, the
    staging/final-authority role separation it makes explicit, and the
    deliberately-divergent regression that fixes the boundary between them.

    AC3: post_comment() must use canonical JSON UTF-8 -> ``gh api --method
    POST ... --input -`` (mirroring patch_comment()'s existing transport),
    with no tempfile / no ``--field body=@file``, and its
    (html_url_or_None, post_status_code, http_status_or_None) return
    contract must be unchanged.

    AC2: the POST-staging digest (of what was just POSTed) and the final
    persisted-body authority digest (of what a direct-GET readback reports)
    must be distinctly named in source, never sharing an authority-like name.

    AC1 (deliberately-divergent regression): a fake gh transport where the
    POST-staging body differs from what a subsequent direct-GET readback
    reports as persisted.
      (a) When the final direct-GET body matches the expected final body,
          binding must PASS even though the staging digest differs from the
          final persisted digest.
      (b) When the final direct-GET body does NOT match the expected final
          body -- even though a staging-side digest coincidentally matches
          some unrelated expected value -- binding must FAIL.
    """

    _COMMENT_ID = 2627001

    def test_post_comment_uses_canonical_json_stdin_transport_no_tempfile(self, monkeypatch):
        captured: dict = {}

        def fake_run(command, *, input=None, **kwargs):
            captured["command"] = command
            captured["input"] = input
            captured["kwargs"] = kwargs
            return MagicMock(
                returncode=0,
                stdout=f"{_ISSUE_URL}#issuecomment-{self._COMMENT_ID}".encode("utf-8"),
                stderr=b"",
            )

        monkeypatch.setattr(_ecs_mod.subprocess, "run", fake_run)

        body = "staged comment body"
        url, post_code, http_status = _ecs_mod.post_comment(_ISSUE_NUMBER, _REPO, body)

        assert (url, post_code, http_status) == (
            f"{_ISSUE_URL}#issuecomment-{self._COMMENT_ID}",
            POST_STATUS_POSTED,
            None,
        )
        command = captured["command"]
        assert command[0:2] == ["gh", "api"]
        assert command[command.index("--method") + 1] == "POST"
        assert command.count(f"repos/{_REPO}/issues/{_ISSUE_NUMBER}/comments") == 1
        assert "--input" in command
        assert command[command.index("--input") + 1] == "-"
        assert "--jq" in command
        assert not any(str(arg).startswith("--field") for arg in command)
        assert not any(str(arg).startswith("body=@") for arg in command)
        assert captured["kwargs"].get("capture_output") is True
        assert "timeout" in captured["kwargs"]
        assert captured["input"] == json.dumps(
            {"body": body}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def test_post_comment_source_has_no_tempfile_or_field_upload(self):
        source = inspect.getsource(_ecs_mod.post_comment)
        assert "import tempfile" not in source
        assert "NamedTemporaryFile" not in source
        assert "--field" not in source
        assert "body=@" not in source

    def test_role_separated_digest_names_are_distinct_in_source(self):
        source = inspect.getsource(_ecs_mod.ensure_contract_snapshot)
        assert "staging_post_body_sha256" in source
        assert "final_persisted_body_sha256" in source
        assert "staging_post_body_sha256" != "final_persisted_body_sha256"

    def test_given_staging_digest_diverges_from_final_persisted_digest_when_get_matches_expected_then_binding_passes(
        self, monkeypatch
    ):
        staged_body = "STAGING representation (never persisted verbatim)"
        final_persisted_body = "FINAL persisted body from direct GET"
        remote = {
            "id": self._COMMENT_ID,
            "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}",
            "html_url": f"{_ISSUE_URL}#issuecomment-{self._COMMENT_ID}",
            "user": {"login": "squne121", "id": 63350259, "type": "User"},
            "author_association": "OWNER",
            "body": final_persisted_body,
        }

        def fake_run(command, *, input=None, **kwargs):
            if "--method" in command:
                assert command[command.index("--method") + 1] == "POST"
                payload = json.loads(input.decode("utf-8"))
                assert payload["body"] == staged_body
                return MagicMock(
                    returncode=0,
                    stdout=remote["html_url"].encode("utf-8"),
                    stderr=b"",
                )
            assert command[-1] == f"repos/{_REPO}/issues/comments/{self._COMMENT_ID}"
            return MagicMock(
                returncode=0, stdout=json.dumps(remote).encode("utf-8"), stderr=b""
            )

        monkeypatch.setattr(_ecs_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            _ecs_mod,
            "verify_controlled_publisher_comment_id_binding",
            _real_verify_controlled_publisher_comment_id_binding,
        )

        url, post_code, http_status = _ecs_mod.post_comment(_ISSUE_NUMBER, _REPO, staged_body)
        assert (post_code, http_status) == (POST_STATUS_POSTED, None)

        # The staging digest (of what was POSTed) intentionally differs from
        # the final persisted digest (of what GET reports as persisted).
        staging_post_body_sha256 = sha256_of(staged_body)
        final_persisted_body_sha256 = sha256_of(final_persisted_body)
        assert staging_post_body_sha256 != final_persisted_body_sha256

        expected_comment_id = _ecs_mod.extract_comment_id_from_url(url)
        bound_ok, binding_err = _ecs_mod.verify_controlled_publisher_comment_id_binding(
            _ISSUE_NUMBER,
            _REPO,
            expected_comment_id,
            expected_body_sha256=final_persisted_body_sha256,
        )
        assert (bound_ok, binding_err) == (True, None)

    def test_staging_digest_coincidental_match_get_diverges_binding_fails(self, monkeypatch):
        coincidental_body = "same text used for both staging and one expected value"
        persisted_body_diverges = "different text actually persisted"
        remote = {
            "id": self._COMMENT_ID,
            "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}",
            "html_url": f"{_ISSUE_URL}#issuecomment-{self._COMMENT_ID}",
            "user": {"login": "squne121", "id": 63350259, "type": "User"},
            "author_association": "OWNER",
            "body": persisted_body_diverges,
        }

        def fake_run(command, *, input=None, **kwargs):
            if "--method" in command:
                payload = json.loads(input.decode("utf-8"))
                assert payload["body"] == coincidental_body
                return MagicMock(
                    returncode=0,
                    stdout=remote["html_url"].encode("utf-8"),
                    stderr=b"",
                )
            return MagicMock(
                returncode=0, stdout=json.dumps(remote).encode("utf-8"), stderr=b""
            )

        monkeypatch.setattr(_ecs_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            _ecs_mod,
            "verify_controlled_publisher_comment_id_binding",
            _real_verify_controlled_publisher_comment_id_binding,
        )

        url, post_code, http_status = _ecs_mod.post_comment(
            _ISSUE_NUMBER, _REPO, coincidental_body
        )
        assert (post_code, http_status) == (POST_STATUS_POSTED, None)

        # A staging-side digest that coincidentally equals some unrelated
        # expected value must never be substituted for the expected final
        # persisted-body digest when binding the final authority check.
        staging_post_body_sha256 = sha256_of(coincidental_body)
        expected_final_body_sha256 = sha256_of("intended final persisted body")
        assert staging_post_body_sha256 != expected_final_body_sha256

        expected_comment_id = _ecs_mod.extract_comment_id_from_url(url)
        bound_ok, binding_err = _ecs_mod.verify_controlled_publisher_comment_id_binding(
            _ISSUE_NUMBER,
            _REPO,
            expected_comment_id,
            expected_body_sha256=expected_final_body_sha256,
        )
        assert bound_ok is False
        assert binding_err == "binding_body_hash_mismatch"

    def test_staging_final_digest_orchestration_regression_via_real_ensure_contract_snapshot(
        self, monkeypatch
    ):
        """Issue #2635 gap fix.

        The prior tests in this class call ``post_comment()`` and
        ``verify_controlled_publisher_comment_id_binding()`` directly,
        without ever going through the real ``ensure_contract_snapshot()``
        orchestration -- so they cannot detect a production wiring
        regression where the PATCH-after binding call is accidentally
        rewired to ``expected_body_sha256=staging_post_body_sha256``
        (instead of ``final_persisted_body_sha256``).

        This test builds a stateful fake ``gh api`` remote (POST creates a
        comment holding the staging body; the subsequent single-comment GET
        and the list-comments GET both reflect whatever is currently
        persisted; PATCH overwrites the persisted body) and drives the REAL
        ``ensure_contract_snapshot()`` end-to-end through it, with the real
        ``verify_controlled_publisher_comment_id_binding()``,
        ``patch_comment()``, and ``verify_snapshot_authority_postcondition()``
        (restored here from this file's success-stub autouse fixture) and
        the real contract-review-result parser module.

        AC2 mutation verification (manual, not committed -- see PR
        description): temporarily changing the PATCH-after
        ``verify_controlled_publisher_comment_id_binding()`` call in
        ``ensure_contract_snapshot.py`` to pass
        ``expected_body_sha256=staging_post_body_sha256`` makes this test
        fail, because the fake remote's persisted body at that point is
        already the final (post-PATCH) body, not the staging body.
        """
        remote_comment_id = 2635901
        remote: dict = {
            "comments": {},
            "next_id": remote_comment_id,
            "staging_body": None,
            "final_body": None,
        }

        def fake_run(command, *, input=None, **kwargs):
            # PR #2639 review (P3): extract the ``repos/...`` endpoint
            # semantically instead of by positional index, so this fake
            # stays correct if production reorders its ``gh api`` argv
            # (e.g. flag placement) without changing the endpoint itself.
            endpoints = [
                arg
                for arg in command[2:]
                if isinstance(arg, str) and arg.startswith("repos/")
            ]
            assert len(endpoints) == 1, command
            endpoint = endpoints[0]

            if "--method" in command:
                method = command[command.index("--method") + 1]
                if method == "POST":
                    assert endpoint == f"repos/{_REPO}/issues/{_ISSUE_NUMBER}/comments"
                    payload = json.loads(input.decode("utf-8"))
                    comment_id = remote["next_id"]
                    remote["next_id"] += 1
                    remote["staging_body"] = payload["body"]
                    remote["comments"][comment_id] = {
                        "id": comment_id,
                        "html_url": f"{_ISSUE_URL}#issuecomment-{comment_id}",
                        "issue_url": (
                            f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}"
                        ),
                        "created_at": "2026-06-13T08:00:00Z",
                        "updated_at": "2026-06-13T08:00:00Z",
                        "body": payload["body"],
                        "user": {
                            "login": "squne121",
                            "id": 63350259,
                            "type": "User",
                        },
                        "author_association": "OWNER",
                    }
                    return MagicMock(
                        returncode=0,
                        stdout=remote["comments"][comment_id]["html_url"].encode(
                            "utf-8"
                        ),
                        stderr=b"",
                    )
                if method == "PATCH":
                    comment_id = int(endpoint.rsplit("/", 1)[-1])
                    payload = json.loads(input.decode("utf-8"))
                    remote["final_body"] = payload["body"]
                    remote["comments"][comment_id]["body"] = payload["body"]
                    remote["comments"][comment_id]["updated_at"] = (
                        "2026-06-13T09:00:00Z"
                    )
                    return MagicMock(returncode=0, stdout=b"", stderr=b"")
                raise AssertionError(f"unexpected gh api --method {method}")

            if "--paginate" in command:
                lines = [
                    json.dumps(
                        {
                            "id": c["id"],
                            "html_url": c["html_url"],
                            "created_at": c["created_at"],
                            "updated_at": c["updated_at"],
                            "body": c["body"],
                            "author": c["user"]["login"],
                            "author_id": c["user"]["id"],
                            "author_type": c["user"]["type"],
                            "author_association": c["author_association"],
                        }
                    )
                    for c in remote["comments"].values()
                ]
                return MagicMock(returncode=0, stdout="\n".join(lines), stderr="")

            m = re.match(
                rf"repos/{re.escape(_REPO)}/issues/comments/(\d+)$", endpoint
            )
            if m:
                comment_id = int(m.group(1))
                c = remote["comments"].get(comment_id)
                if c is None:
                    return MagicMock(returncode=1, stdout="", stderr="HTTP 404")
                payload = {
                    "id": c["id"],
                    "issue_url": c["issue_url"],
                    "html_url": c["html_url"],
                    "user": c["user"],
                    "author_association": c["author_association"],
                    "body": c["body"],
                }
                return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

            raise AssertionError(f"unexpected gh api command: {command}")

        monkeypatch.setattr(_ecs_mod.subprocess, "run", fake_run)
        # Restore the real implementations this file's autouse fixture
        # otherwise stubs to success -- this test's whole point is to
        # exercise the real POST/PATCH/GET reconciliation, not a stub.
        monkeypatch.setattr(
            _ecs_mod,
            "verify_controlled_publisher_comment_id_binding",
            _real_verify_controlled_publisher_comment_id_binding,
        )
        monkeypatch.setattr(_ecs_mod, "patch_comment", _real_patch_comment)
        monkeypatch.setattr(
            _ecs_mod,
            "verify_snapshot_authority_postcondition",
            _real_verify_snapshot_authority_postcondition,
        )

        review_result = _make_review_result("go")

        with patch.object(
            _ecs_mod,
            "_import_parser_module",
            return_value=_real_parser_mod_for_fp_tests,
        ):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(review_result, None),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="auto",
                        do_post=True,
                    )

        assert result["status"] == "ok", result
        assert result["source"] == "materialized_go"
        assert (
            result["contract_snapshot_url"]
            == remote["comments"][remote_comment_id]["html_url"]
        )

        # The precondition the AC2 mutation check depends on: the POST-time
        # staging body and the post-PATCH final persisted body must actually
        # be different byte sequences.
        assert remote["staging_body"] is not None
        assert remote["final_body"] is not None
        assert remote["staging_body"] != remote["final_body"]
        assert sha256_of(remote["staging_body"]) != sha256_of(remote["final_body"])
        assert remote["comments"][remote_comment_id]["body"] == remote["final_body"]


# ---------------------------------------------------------------------------
# Issue #3004: post-publish Issue.updatedAt race (false-stale on a verified
# self-posted snapshot) -- regressions run the REAL ensure_contract_snapshot(),
# REAL verify_snapshot_authority_postcondition(), REAL canonical parser, REAL
# fetch_issue_snapshot()/capture_base_ref_and_sha()/patch_comment()/
# post_comment()/verify_controlled_publisher_comment_id_binding(). Only the
# external ``gh`` subprocess boundary and the remote GitHub state are faked.
# ---------------------------------------------------------------------------

_RACE_U0 = "2026-06-13T08:00:00Z"
_RACE_U1 = "2026-06-13T08:00:05Z"
_RACE_U2 = "2026-06-13T08:00:10Z"
_RACE_COMMENT_ID = 3004001
_RACE_TRUSTED_USER = {"login": "squne121", "id": 63350259, "type": "User"}
_RACE_UNTRUSTED_USER = {"login": "mallory", "id": 4242, "type": "User"}


def _race_blocked_body() -> str:
    return (
        "```yaml\n"
        "CONTRACT_REVIEW_RESULT_V1:\n"
        "  status: blocked\n"
        '  generated_at: "2026-06-13T10:00:00Z"\n'
        "  generated_by: issue-contract-review\n"
        f"  issue_url: {_ISSUE_URL}\n"
        "```\n"
    )


def _race_not_ready_go_body() -> str:
    """Schema-valid trusted ``go`` without a fingerprint (not fingerprint-ready)."""
    return (
        "```yaml\n"
        "CONTRACT_REVIEW_RESULT_V1:\n"
        "  status: go\n"
        '  generated_at: "2026-06-13T11:00:00Z"\n'
        "  generated_by: issue-contract-review\n"
        f"  issue_url: {_ISSUE_URL}\n"
        f'  body_sha256: "{_SAMPLE_BODY_SHA256}"\n'
        "```\n"
    )


class _PostPublishRaceRemote:
    """Stateful fake of the ``gh`` boundary for one Issue.

    ``updated_at_for(n)`` gives the Issue.updatedAt returned by the n-th
    ``gh issue view`` read after the fingerprint PATCH (n starts at 1 = the
    authority anchor read).  ``hooks[n]`` runs just before that read answers,
    so a test can inject a remote mutation at an exact point of the sequence.
    """

    def __init__(self, updated_at_for=None):
        self.body = _SAMPLE_BODY
        self.base_sha = "a" * 40
        self.base_ref = "main"
        self.updated_at_for = updated_at_for or (lambda n: _RACE_U0)
        self.hooks: dict = {}
        self.comments: dict = {}
        self.next_id = _RACE_COMMENT_ID
        self.post_count = 0
        self.patch_count = 0
        self.patched_comment_ids: list = []
        self.armed = False
        self.read_n = 0
        self.fail_issue_view_at: set = set()
        self.fail_comment_list = False

    def arm(self):
        self.armed = True
        self.read_n = 0

    def add_comment(self, *, body, user, association, created_at, comment_id=None):
        cid = comment_id if comment_id is not None else self.next_id
        if comment_id is None:
            self.next_id += 1
        self.comments[cid] = {
            "id": cid,
            "html_url": f"{_ISSUE_URL}#issuecomment-{cid}",
            "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}",
            "created_at": created_at,
            "updated_at": created_at,
            "body": body,
            "user": user,
            "author_association": association,
        }
        return cid

    def add_trusted_blocked(self):
        return self.add_comment(
            body=_race_blocked_body(),
            user=_RACE_TRUSTED_USER,
            association="OWNER",
            created_at="2026-06-13T10:00:00Z",
            comment_id=9001,
        )

    def add_trusted_not_ready_go(self):
        return self.add_comment(
            body=_race_not_ready_go_body(),
            user=_RACE_TRUSTED_USER,
            association="OWNER",
            created_at="2026-06-13T11:00:00Z",
            comment_id=9004,
        )

    def add_untrusted_blocked(self):
        return self.add_comment(
            body=_race_blocked_body(),
            user=_RACE_UNTRUSTED_USER,
            association="NONE",
            created_at="2026-06-13T10:00:00Z",
            comment_id=9002,
        )

    def add_ordinary_comment(self):
        return self.add_comment(
            body="ordinary discussion comment",
            user=_RACE_UNTRUSTED_USER,
            association="NONE",
            created_at="2026-06-13T10:30:00Z",
            comment_id=9003,
        )

    def own_comment_ids(self):
        return [
            cid
            for cid, c in self.comments.items()
            if "CONTRACT_REVIEW_RESULT_V1" in c["body"]
            and c["user"] == _RACE_TRUSTED_USER
        ]

    def run(self, command, *, input=None, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            if self.armed:
                self.read_n += 1
                hook = self.hooks.get(self.read_n)
                if hook is not None:
                    hook(self)
                if self.read_n in self.fail_issue_view_at:
                    return MagicMock(returncode=1, stdout="", stderr="HTTP 502")
                updated_at = self.updated_at_for(self.read_n)
            else:
                updated_at = _RACE_U0
            return MagicMock(
                returncode=0,
                stdout=json.dumps({"body": self.body, "updatedAt": updated_at}),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            payload = {
                "data": {
                    "repository": {
                        "defaultBranchRef": {
                            "name": self.base_ref,
                            "target": {"oid": self.base_sha},
                        }
                    }
                }
            }
            return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

        endpoints = [
            arg
            for arg in command[2:]
            if isinstance(arg, str) and arg.startswith("repos/")
        ]
        assert len(endpoints) == 1, command
        endpoint = endpoints[0]

        if "--method" in command:
            method = command[command.index("--method") + 1]
            if method == "POST":
                assert endpoint == f"repos/{_REPO}/issues/{_ISSUE_NUMBER}/comments"
                self.post_count += 1
                payload = json.loads(input.decode("utf-8"))
                cid = self.add_comment(
                    body=payload["body"],
                    user=_RACE_TRUSTED_USER,
                    association="OWNER",
                    created_at="2026-06-13T08:00:00Z",
                )
                return MagicMock(
                    returncode=0,
                    stdout=self.comments[cid]["html_url"].encode("utf-8"),
                    stderr=b"",
                )
            if method == "PATCH":
                cid = int(endpoint.rsplit("/", 1)[-1])
                self.patch_count += 1
                self.patched_comment_ids.append(cid)
                self.comments[cid]["body"] = json.loads(input.decode("utf-8"))["body"]
                self.comments[cid]["updated_at"] = "2026-06-13T09:00:00Z"
                self.arm()
                return MagicMock(returncode=0, stdout=b"", stderr=b"")
            raise AssertionError(f"unexpected gh api --method {method}")

        if "--paginate" in command:
            if self.armed and self.fail_comment_list:
                return MagicMock(returncode=1, stdout="", stderr="HTTP 502")
            lines = [
                json.dumps(
                    {
                        "id": c["id"],
                        "html_url": c["html_url"],
                        "created_at": c["created_at"],
                        "updated_at": c["updated_at"],
                        "body": c["body"],
                        "author": c["user"]["login"],
                        "author_id": c["user"]["id"],
                        "author_type": c["user"]["type"],
                        "author_association": c["author_association"],
                    }
                )
                for c in self.comments.values()
            ]
            return MagicMock(returncode=0, stdout="\n".join(lines), stderr="")

        m = re.match(rf"repos/{re.escape(_REPO)}/issues/comments/(\d+)$", endpoint)
        if m:
            c = self.comments.get(int(m.group(1)))
            if c is None:
                return MagicMock(returncode=1, stdout="", stderr="HTTP 404")
            payload = {
                "id": c["id"],
                "issue_url": c["issue_url"],
                "html_url": c["html_url"],
                "user": c["user"],
                "author_association": c["author_association"],
                "body": c["body"],
            }
            return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"unexpected gh command: {command}")


def _run_ensure_against_race_remote(monkeypatch, remote, *, mode="auto", do_post=True):
    """Drive the REAL ensure_contract_snapshot() through the fake remote."""
    monkeypatch.setattr(_ecs_mod.subprocess, "run", remote.run)
    monkeypatch.setattr(
        _ecs_mod,
        "verify_controlled_publisher_comment_id_binding",
        _real_verify_controlled_publisher_comment_id_binding,
    )
    monkeypatch.setattr(_ecs_mod, "patch_comment", _real_patch_comment)
    monkeypatch.setattr(
        _ecs_mod,
        "verify_snapshot_authority_postcondition",
        _real_verify_snapshot_authority_postcondition,
    )
    monkeypatch.setattr(
        _ecs_mod, "capture_base_ref_and_sha", _real_capture_base_ref_and_sha
    )
    with patch.object(
        _ecs_mod,
        "_import_parser_module",
        return_value=_real_parser_mod_for_fp_tests,
    ):
        with patch.object(
            _ecs_mod,
            "run_contract_review_once",
            return_value=(_make_review_result("go"), None),
        ):
            return ensure_contract_snapshot(
                issue_number=_ISSUE_NUMBER, repo=_REPO, mode=mode, do_post=do_post
            )


def _flip_after_anchor(n):
    """U0 on the anchor read (n == 1), U1 on every later read (U0 -> U1 -> U1)."""
    return _RACE_U0 if n == 1 else _RACE_U1


def _flip_twice_then_stable(n):
    """U0 -> U1 -> U2 -> U2: the timestamp still changes on the first re-read."""
    return {1: _RACE_U0, 2: _RACE_U1}.get(n, _RACE_U2)


def _assert_fail_closed_single_publish(result, remote):
    assert result["status"] == "stale_or_conflicting_snapshot", result
    assert result["contract_snapshot_url"] is None
    assert remote.post_count == 1
    assert remote.patch_count == 1


class TestPostPublishUpdatedAtRace:
    """Issue #3004: a verified self-posted go must not be false-stale because
    of an Issue.updatedAt read-time difference, while every real conflict
    (later trusted blocked, body/comment/fingerprint/base drift, incomplete
    readback, unstable timestamps) stays fail-closed."""

    def test_post_publish_updated_at_race_u0_u1_u1_returns_ok_without_republish(
        self, monkeypatch
    ):
        remote = _PostPublishRaceRemote(updated_at_for=_flip_after_anchor)

        result = _run_ensure_against_race_remote(monkeypatch, remote)

        assert result["status"] == "ok", result
        assert result["source"] == "materialized_go"
        only_id = remote.own_comment_ids()
        assert len(only_id) == 1
        assert result["contract_snapshot_url"] == f"{_ISSUE_URL}#issuecomment-{only_id[0]}"
        assert remote.post_count == 1
        assert remote.patch_count == 1
        assert remote.patched_comment_ids == only_id
        assert len(remote.comments) == 1

    def test_post_publish_updated_at_race_later_trusted_blocked_fails_with_matching_updated_at(
        self, monkeypatch
    ):
        remote = _PostPublishRaceRemote(updated_at_for=lambda n: _RACE_U0)
        remote.hooks[1] = lambda r: r.add_trusted_blocked()

        result = _run_ensure_against_race_remote(monkeypatch, remote)

        _assert_fail_closed_single_publish(result, remote)
        assert any("authority_latest_trusted_result_blocked" in e for e in result["errors"]), result
        assert not any("updated_at_mismatch" in e for e in result["errors"]), result

    def test_post_publish_updated_at_race_later_trusted_blocked_fails_during_reconciliation(
        self, monkeypatch
    ):
        remote = _PostPublishRaceRemote(updated_at_for=_flip_after_anchor)
        remote.hooks[3] = lambda r: r.add_trusted_blocked()

        result = _run_ensure_against_race_remote(monkeypatch, remote)

        _assert_fail_closed_single_publish(result, remote)
        assert any("authority_latest_trusted_result_blocked" in e for e in result["errors"]), result

    def test_post_publish_updated_at_race_ordinary_and_untrusted_blocked_comments_do_not_reject(
        self, monkeypatch
    ):
        for timeline in (lambda n: _RACE_U0, _flip_after_anchor):
            remote = _PostPublishRaceRemote(updated_at_for=timeline)

            def inject(r):
                r.add_ordinary_comment()
                r.add_untrusted_blocked()

            remote.hooks[1] = inject

            result = _run_ensure_against_race_remote(monkeypatch, remote)

            assert result["status"] == "ok", result
            assert result["source"] == "materialized_go"
            assert remote.post_count == 1
            assert remote.patch_count == 1

    def test_post_publish_updated_at_race_real_drift_is_not_cancelled_by_later_match(
        self, monkeypatch
    ):
        def drift_body(r):
            r.body = _SAMPLE_BODY + "\nedited by someone else\n"

        def restore_body(r):
            r.body = _SAMPLE_BODY

        def drift_comment_body(r):
            cid = r.own_comment_ids()[0]
            r.comments[cid]["body"] += "\n<!-- tampered -->\n"

        def drift_base(r):
            r.base_sha = "b" * 40

        def delete_comment(r):
            r.comments.pop(r.own_comment_ids()[0])

        scenarios = {
            "body_drift_first_recheck": (
                {2: drift_body},
                "authority_issue_body_sha256_mismatch",
            ),
            # body changes during reconciliation and is restored right after (ABA)
            "body_aba": (
                {3: drift_body, 4: restore_body},
                "authority_issue_body_sha256_mismatch",
            ),
            "comment_body_drift": ({3: drift_comment_body}, "authority_comment_body_sha256_mismatch"),
            "base_drift": ({3: drift_base}, "authority_base_binding_drift"),
            "base_drift_then_restored": (
                {3: drift_base, 4: lambda r: setattr(r, "base_sha", "a" * 40)},
                "authority_base_binding_drift",
            ),
            "comment_missing": ({3: delete_comment}, "authority_comment_missing"),
        }
        for name, (hooks, reason) in scenarios.items():
            remote = _PostPublishRaceRemote(updated_at_for=_flip_after_anchor)
            remote.hooks.update(hooks)

            result = _run_ensure_against_race_remote(monkeypatch, remote)

            assert result["status"] == "stale_or_conflicting_snapshot", (name, result)
            assert result["contract_snapshot_url"] is None, name
            assert any(reason in e for e in result["errors"]), (name, result["errors"])
            assert remote.post_count == 1, name
            assert remote.patch_count == 1, name

    def test_post_publish_updated_at_race_non_time_drift_during_changing_timestamp_fails(
        self, monkeypatch
    ):
        # U0 -> U1 -> U2 -> U2: the drift is injected on a read whose updatedAt
        # differs from the expected one, and restored on the next read.
        def drift_comment_body(r):
            r.comments[r.own_comment_ids()[0]]["body"] += "\n<!-- tampered -->\n"

        def restore_comment_body(r):
            cid = r.own_comment_ids()[0]
            r.comments[cid]["body"] = r.comments[cid]["body"].replace(
                "\n<!-- tampered -->\n", ""
            )

        scenarios = {
            "base_drift_restored": (
                {
                    3: lambda r: setattr(r, "base_sha", "b" * 40),
                    4: lambda r: setattr(r, "base_sha", "a" * 40),
                },
                "authority_base_binding_drift",
            ),
            "comment_body_drift_restored": (
                {3: drift_comment_body, 4: restore_comment_body},
                "authority_comment_body_sha256_mismatch",
            ),
            "trusted_blocked_during_time_change": (
                {3: lambda r: r.add_trusted_blocked()},
                "authority_latest_trusted_result_blocked",
            ),
        }
        for name, (hooks, reason) in scenarios.items():
            remote = _PostPublishRaceRemote(updated_at_for=_flip_twice_then_stable)
            remote.hooks.update(hooks)

            result = _run_ensure_against_race_remote(monkeypatch, remote)

            _assert_fail_closed_single_publish(result, remote)
            assert any(reason in e for e in result["errors"]), (name, result["errors"])

    def test_post_publish_updated_at_race_u0_u1_u2_u2_without_drift_returns_ok(
        self, monkeypatch
    ):
        remote = _PostPublishRaceRemote(updated_at_for=_flip_twice_then_stable)

        result = _run_ensure_against_race_remote(monkeypatch, remote)

        assert result["status"] == "ok", result
        assert (remote.post_count, remote.patch_count) == (1, 1)

    def test_post_publish_updated_at_race_not_ready_later_go_does_not_hide_trusted_blocked(
        self, monkeypatch
    ):
        # [G, B, P]: G = materialized go, B = later trusted blocked, P = later
        # trusted go that is not fingerprint-ready.  P is not adoptable and must
        # not cancel B.
        for timeline in (lambda n: _RACE_U0, _flip_after_anchor):
            remote = _PostPublishRaceRemote(updated_at_for=timeline)

            def inject(r):
                r.add_trusted_blocked()
                r.add_trusted_not_ready_go()

            remote.hooks[1] = inject

            result = _run_ensure_against_race_remote(monkeypatch, remote)

            _assert_fail_closed_single_publish(result, remote)
            assert any(
                "authority_latest_trusted_result_blocked" in e for e in result["errors"]
            ), result["errors"]

    def test_post_publish_updated_at_race_not_ready_go_without_blocked_does_not_reject(
        self, monkeypatch
    ):
        # Control: a later not-ready trusted go alone (no blocked) is ignored.
        for timeline in (lambda n: _RACE_U0, _flip_after_anchor):
            remote = _PostPublishRaceRemote(updated_at_for=timeline)
            remote.hooks[1] = lambda r: r.add_trusted_not_ready_go()

            result = _run_ensure_against_race_remote(monkeypatch, remote)

            assert result["status"] == "ok", result
            assert (remote.post_count, remote.patch_count) == (1, 1)

    def test_post_publish_updated_at_race_incomplete_readback_fails_closed(
        self, monkeypatch
    ):
        remote = _PostPublishRaceRemote(updated_at_for=_flip_after_anchor)
        remote.fail_issue_view_at = {3}
        result = _run_ensure_against_race_remote(monkeypatch, remote)
        _assert_fail_closed_single_publish(result, remote)
        assert any("authority_issue_readback_failed" in e for e in result["errors"])

        remote = _PostPublishRaceRemote(updated_at_for=_flip_after_anchor)
        remote.hooks[3] = lambda r: setattr(r, "fail_comment_list", True)
        result = _run_ensure_against_race_remote(monkeypatch, remote)
        _assert_fail_closed_single_publish(result, remote)
        assert any("authority_comments_readback_failed" in e for e in result["errors"])

    def test_post_publish_updated_at_race_reconciliation_cap_reached_fails_closed(
        self, monkeypatch
    ):
        remote = _PostPublishRaceRemote(
            updated_at_for=lambda n: f"2026-06-13T08:{n // 60:02d}:{n % 60:02d}Z"
        )

        result = _run_ensure_against_race_remote(monkeypatch, remote)

        _assert_fail_closed_single_publish(result, remote)
        assert any("authority_issue_updated_at_mismatch" in e for e in result["errors"])
        # bounded: the anchor read plus a small fixed number of re-reads
        assert remote.read_n <= 10, remote.read_n

    def test_post_publish_updated_at_race_check_only_existing_go_keeps_stale_rejection(
        self, monkeypatch
    ):
        # Phase 1: materialize a real trusted go with a stable timeline.
        remote = _PostPublishRaceRemote(updated_at_for=lambda n: _RACE_U0)
        first = _run_ensure_against_race_remote(monkeypatch, remote)
        assert first["status"] == "ok", first
        assert remote.post_count == 1 and remote.patch_count == 1

        # Phase 2: check-only reuse where the authority re-read sees a different
        # updatedAt than the two reads that selected the existing go.
        remote.updated_at_for = lambda n: _RACE_U0 if n <= 2 else _RACE_U1
        remote.arm()
        second = _run_ensure_against_race_remote(
            monkeypatch, remote, mode="check-only", do_post=False
        )

        assert second["status"] != "ok", second
        assert second["contract_snapshot_url"] is None
        assert second["source"] != "existing_go"
        assert remote.post_count == 1 and remote.patch_count == 1
        assert len(remote.own_comment_ids()) == 1

    def test_post_publish_updated_at_race_publish_counts_and_target_comment_unchanged(
        self, monkeypatch
    ):
        remote = _PostPublishRaceRemote(updated_at_for=_flip_after_anchor)

        result = _run_ensure_against_race_remote(monkeypatch, remote)

        assert result["status"] == "ok", result
        assert (remote.post_count, remote.patch_count) == (1, 1)
        assert remote.patched_comment_ids == [_RACE_COMMENT_ID]
        assert list(remote.comments) == [_RACE_COMMENT_ID]
        assert result["contract_snapshot_url"].endswith(f"#issuecomment-{_RACE_COMMENT_ID}")


# ---------------------------------------------------------------------------
# Issue #3012: a trusted fingerprint-NON-ready go that is newer than a trusted
# blocked must not hide that blocked result from the INITIAL existing_go
# selection of the REAL ensure_contract_snapshot().  The REAL shared parser is
# used (``_import_parser_module`` is not a MagicMock) and only the ``gh``
# subprocess boundary is faked via ``_PostPublishRaceRemote``.  The
# post-publication verifier stays under the autouse stub, so these tests do
# not depend on ``stale_or_conflicting_snapshot``.
# Timeline notation: G = fingerprint-ready trusted go, B = trusted blocked,
# P = schema-valid trusted go that is NOT fingerprint-ready.
# ---------------------------------------------------------------------------

_NR_T = "2026-06-13T07:00:0{}Z"


def _nr_add_ready_go(remote, cid, created_at, *, base_sha="a" * 40, user=None, association="OWNER"):
    fingerprint = _ecs_mod.compute_expected_contract_fingerprint(
        issue_number=_ISSUE_NUMBER,
        contract_source_id=str(cid),
        contract_body_sha256=_SAMPLE_BODY_SHA256,
        allowed_paths=["tracked.txt"],
        base_ref="main",
        base_sha_at_snapshot=base_sha,
    )
    body = _ecs_mod._build_contract_review_comment(
        issue_number=_ISSUE_NUMBER,
        repo=_REPO,
        review_result=_make_review_result("go"),
        idempotency_marker="<!-- marker -->",
        body_sha256=_SAMPLE_BODY_SHA256,
        expected_contract_fingerprint=fingerprint,
    )
    return remote.add_comment(
        body=body,
        user=user or _RACE_TRUSTED_USER,
        association=association,
        created_at=created_at,
        comment_id=cid,
    )


def _nr_add_blocked(remote, cid, created_at, *, user=None, association="OWNER"):
    return remote.add_comment(
        body=_race_blocked_body(),
        user=user or _RACE_TRUSTED_USER,
        association=association,
        created_at=created_at,
        comment_id=cid,
    )


def _nr_add_nonready_go(remote, cid, created_at):
    return remote.add_comment(
        body=_race_not_ready_go_body(),
        user=_RACE_TRUSTED_USER,
        association="OWNER",
        created_at=created_at,
        comment_id=cid,
    )


def _nr_assert_fixture_shape(remote, *, go_id=None, blocked_id=None, nonready_id=None):
    """After the REAL parser: G/B/P are trusted, schema-valid; P is not fingerprint-ready."""
    comments = [
        {
            "id": c["id"],
            "html_url": c["html_url"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "body": c["body"],
            "author": c["user"]["login"],
            "author_id": c["user"]["id"],
            "author_type": c["user"]["type"],
            "author_association": c["author_association"],
        }
        for c in remote.comments.values()
    ]
    parsed = _real_parser_mod_for_fp_tests.parse_contract_review_results(
        comments, expected_issue_url=_ISSUE_URL
    )
    by_id = {r["comment_id"]: r for r in parsed}
    for cid in (go_id, blocked_id, nonready_id):
        if cid is not None:
            assert by_id[cid]["is_trusted_author"] is True
    if go_id is not None:
        assert by_id[go_id]["status"] == "go"
        assert by_id[go_id]["is_fingerprint_ready"] is True
        assert is_go_current(by_id[go_id], _SAMPLE_BODY_SHA256)
    if blocked_id is not None:
        assert by_id[blocked_id]["status"] == "blocked"
    if nonready_id is not None:
        assert by_id[nonready_id]["status"] == "go"
        assert by_id[nonready_id]["is_fingerprint_ready"] is False


def _nr_run_ensure(monkeypatch, remote, *, mode="check-only"):
    """REAL ensure_contract_snapshot(), REAL parser, ``gh`` boundary faked; verifier stays stubbed."""
    monkeypatch.setattr(_ecs_mod.subprocess, "run", remote.run)
    with patch.object(
        _ecs_mod, "_import_parser_module", return_value=_real_parser_mod_for_fp_tests
    ):
        return ensure_contract_snapshot(
            issue_number=_ISSUE_NUMBER, repo=_REPO, mode=mode, do_post=False
        )


def _nr_url(cid):
    return f"{_ISSUE_URL}#issuecomment-{cid}"


def _nr_assert_latest_blocked(result, blocked_id):
    assert result["status"] == "blocked_needs_refinement", result
    assert result["source"] == "latest_blocked", result
    assert result["contract_snapshot_url"] == _nr_url(blocked_id), result


def test_nonready_go_after_blocked_initial_selection_returns_latest_blocked(monkeypatch):
    remote = _PostPublishRaceRemote()
    _nr_add_ready_go(remote, 8101, _NR_T.format(1))
    _nr_add_blocked(remote, 8102, _NR_T.format(2))
    _nr_add_nonready_go(remote, 8103, _NR_T.format(3))
    _nr_assert_fixture_shape(remote, go_id=8101, blocked_id=8102, nonready_id=8103)

    result = _nr_run_ensure(monkeypatch, remote)

    _nr_assert_latest_blocked(result, 8102)
    assert remote.post_count == 0 and remote.patch_count == 0


def test_nonready_go_after_blocked_positive_controls_ready_go_still_adopted(monkeypatch):
    for with_p in (False, True):
        remote = _PostPublishRaceRemote()
        _nr_add_ready_go(remote, 8101, _NR_T.format(1))
        if with_p:
            _nr_add_nonready_go(remote, 8103, _NR_T.format(3))
        _nr_assert_fixture_shape(
            remote, go_id=8101, nonready_id=8103 if with_p else None
        )

        result = _nr_run_ensure(monkeypatch, remote)

        assert result["status"] == "ok", result
        assert result["source"] == "existing_go", result
        assert result["contract_snapshot_url"] == _nr_url(8101)


def test_nonready_go_after_blocked_blocked_directly_after_ready_go_is_blocked(monkeypatch):
    remote = _PostPublishRaceRemote()
    _nr_add_ready_go(remote, 8101, _NR_T.format(1))
    _nr_add_blocked(remote, 8102, _NR_T.format(2))
    _nr_assert_fixture_shape(remote, go_id=8101, blocked_id=8102)

    _nr_assert_latest_blocked(_nr_run_ensure(monkeypatch, remote), 8102)


def test_nonready_go_after_blocked_later_ready_go_beats_earlier_blocked(monkeypatch):
    remote = _PostPublishRaceRemote()
    _nr_add_ready_go(remote, 8101, _NR_T.format(1))
    _nr_add_blocked(remote, 8102, _NR_T.format(2))
    _nr_add_ready_go(remote, 8104, _NR_T.format(4))
    _nr_assert_fixture_shape(remote, go_id=8104, blocked_id=8102)

    result = _nr_run_ensure(monkeypatch, remote)

    assert result["status"] == "ok", result
    assert result["source"] == "existing_go", result
    assert result["contract_snapshot_url"] == _nr_url(8104)


def test_nonready_go_after_blocked_blocked_without_ready_go_matches_blocked_alone(monkeypatch):
    for with_p in (False, True):
        remote = _PostPublishRaceRemote()
        _nr_add_blocked(remote, 8102, _NR_T.format(2))
        if with_p:
            _nr_add_nonready_go(remote, 8103, _NR_T.format(3))
        _nr_assert_fixture_shape(
            remote, blocked_id=8102, nonready_id=8103 if with_p else None
        )

        _nr_assert_latest_blocked(_nr_run_ensure(monkeypatch, remote), 8102)


def test_nonready_go_after_blocked_untrusted_blocked_and_plain_comments_do_not_reject(monkeypatch):
    remote = _PostPublishRaceRemote()
    _nr_add_ready_go(remote, 8101, _NR_T.format(1))
    _nr_add_blocked(
        remote, 8102, _NR_T.format(2), user=_RACE_UNTRUSTED_USER, association="NONE"
    )
    remote.add_comment(
        body="ordinary discussion",
        user=_RACE_TRUSTED_USER,
        association="OWNER",
        created_at=_NR_T.format(3),
        comment_id=8105,
    )

    result = _nr_run_ensure(monkeypatch, remote)

    assert result["status"] == "ok", result
    assert result["contract_snapshot_url"] == _nr_url(8101)


def test_nonready_go_after_blocked_pending_only_comment_is_not_a_go(monkeypatch):
    remote = _PostPublishRaceRemote()
    remote.add_comment(
        body=_ecs_mod._build_contract_review_comment(
            issue_number=_ISSUE_NUMBER,
            repo=_REPO,
            review_result=_make_review_result("go"),
            idempotency_marker="<!-- marker -->",
            body_sha256=_SAMPLE_BODY_SHA256,
            expected_contract_fingerprint=None,
        ),
        user=_RACE_TRUSTED_USER,
        association="OWNER",
        created_at=_NR_T.format(2),
        comment_id=8106,
    )

    result = _nr_run_ensure(monkeypatch, remote)

    assert result["status"] == "human_judgment", result
    assert result["contract_snapshot_url"] is None


def test_nonready_go_after_blocked_binding_mismatch_keeps_ready_go_rejected(monkeypatch):
    drifted_base = _PostPublishRaceRemote()
    _nr_add_ready_go(drifted_base, 8101, _NR_T.format(1), base_sha="f" * 40)
    drifted = _nr_run_ensure(monkeypatch, drifted_base)
    assert drifted["status"] == "human_judgment", drifted
    assert drifted["contract_snapshot_url"] is None

    edited_body = _PostPublishRaceRemote()
    edited_body.body = _SAMPLE_BODY + "edited after snapshot\n"
    _nr_add_ready_go(edited_body, 8101, _NR_T.format(1))
    edited = _nr_run_ensure(monkeypatch, edited_body)
    assert edited["status"] == "human_judgment", edited
    assert edited["contract_snapshot_url"] is None


def test_nonready_go_after_blocked_input_order_does_not_change_result(monkeypatch):
    remote = _PostPublishRaceRemote()
    _nr_add_nonready_go(remote, 8103, _NR_T.format(3))
    _nr_add_blocked(remote, 8102, _NR_T.format(2))
    _nr_add_ready_go(remote, 8101, _NR_T.format(1))
    assert list(remote.comments) == [8103, 8102, 8101]

    _nr_assert_latest_blocked(_nr_run_ensure(monkeypatch, remote), 8102)


def test_nonready_go_after_blocked_same_created_at_uses_numeric_comment_id(monkeypatch):
    same = _NR_T.format(5)
    blocked_wins = _PostPublishRaceRemote()
    _nr_add_ready_go(blocked_wins, 8101, _NR_T.format(1))
    _nr_add_ready_go(blocked_wins, 8110, same)
    _nr_add_blocked(blocked_wins, 8120, same)
    _nr_assert_latest_blocked(_nr_run_ensure(monkeypatch, blocked_wins), 8120)

    go_wins = _PostPublishRaceRemote()
    _nr_add_ready_go(go_wins, 8101, _NR_T.format(1))
    _nr_add_blocked(go_wins, 809, same)
    _nr_add_ready_go(go_wins, 8110, same)
    result = _nr_run_ensure(monkeypatch, go_wins)
    assert result["status"] == "ok", result
    assert result["contract_snapshot_url"] == _nr_url(8110)
