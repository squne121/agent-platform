from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
_GUARDS = _ROOT / "scripts/agent-guards"
sys.path.insert(0, str(_GUARDS))
_EXECUTOR = _GUARDS / "controlled_skill_mutation_exec.py"
_spec = importlib.util.spec_from_file_location("human_history_controlled_executor", _EXECUTOR)
executor = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(executor)
TRUSTED_REPO = executor.TRUSTED_REPO


def _marker(seed: str = "identity") -> str:
    return f"<!-- loop-protocol/human-history:v1:sha256:{hashlib.sha256(seed.encode()).hexdigest()} -->"


def _body(
    visible: str = "日本語の公開可能な履歴", marker: str | None = None, newline: str = "\n"
) -> str:
    return f"{visible}{newline}{marker or _marker()}{newline}"


def test_marker_ownership_uniqueness_create_noop_patch_and_readback_reconciliation_contract():
    marker = _marker()
    parsed, error = executor._parse_human_history_marker_source(
        _body(marker=marker, newline="\r\n")
    )
    assert error == ""
    assert parsed and parsed["marker"] == marker
    assert parsed["content_digest"] == hashlib.sha256("日本語の公開可能な履歴".encode()).hexdigest()

    # Every raw namespace occurrence is a candidate and malformed prose fails
    # before content normalization or ownership logic.
    malformed = [
        "text loop-protocol/human-history",
        _body(marker="<!-- loop-protocol/human-history:v2:sha256:" + "a" * 64 + " -->"),
        _body(marker="<!-- loop-protocol/human-history:v1:sha256:" + "A" * 64 + " -->"),
        _body(marker=" <!-- loop-protocol/human-history:v1:sha256:" + "a" * 64 + " -->"),
        _body(marker="<!-- loop-protocol/human-history:v1:sha256:" + "a" * 64 + " --> trailing"),
        _body(marker="<!-- loop-protocol/human-history:v1:sha256:" + "a" * 64 + " -->") + "later\n",
        _body(marker=marker) + marker,
    ]
    for value in malformed:
        assert executor._parse_human_history_marker_source(value)[0] is None

    # Terminal newline variants normalize identically; other Unicode bytes do
    # not. BOM is input and is neither stripped nor normalized.
    canonical = _body(marker=marker)
    no_terminal = canonical[:-1]
    extra_terminal = canonical + "\n"
    for value in (canonical, no_terminal, extra_terminal, canonical.replace("\n", "\r\n")):
        parsed_value, parse_error = executor._parse_human_history_marker_source(value)
        assert parse_error == "" and parsed_value
        assert (
            parsed_value["content_digest"]
            == hashlib.sha256("日本語の公開可能な履歴".encode()).hexdigest()
        )
    bom, bom_error = executor._parse_human_history_marker_source(
        _body("﻿日本語の公開可能な履歴", marker)
    )
    assert bom_error == "" and bom
    assert bom["content_digest"] != parsed["content_digest"]

    # The full handler routes same identity/digest to noop and changed visible
    # content to PATCH. A foreign valid marker is never a mutation target.
    # Issue #1908 fix_delta HIGH-1: the handler now requires a rendered
    # `- phase:` line before any create/PATCH/noop decision, so this body
    # (unlike `canonical` above, which only exercises marker/digest
    # mechanics) carries a known non-post-PR phase -- no PR-head recheck is
    # required for it, and it stays a pure marker-ownership/noop exercise.
    handler_body = _body("日本語の公開可能な履歴\n- phase: review-complete", marker=marker)
    args = SimpleNamespace(
        issue_number=1908,
        repo=TRUSTED_REPO,
        command_id="issue_comment.publish",
        dry_run=False,
    )
    calls: list[str] = []
    remote = {
        "body": handler_body,
        "url": "https://github.com/x/y/issues/1#issuecomment-42",
        "id": "x",
        "author": {"login": "writer"},
    }
    data = {"comment_body": handler_body, "marker": marker}
    readback = {
        "comment_id": "x",
        "comment_url": "url",
        "identity_sha256": "a" * 64,
        "content_digest": parsed["content_digest"],
    }
    with (
        patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)),
        patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")),
        patch.object(executor, "_list_issue_comments", return_value=([remote], "")),
        patch.object(executor, "_human_history_readback", return_value=(readback, "")),
        patch.object(executor, "_check_no_tracked_changes", return_value=[]),
    ):
        assert (
            executor._run_human_history_comment_publish(
                args,
                data,
                "/bin/gh",
                lambda *a, **k: 1,
                lambda value: calls.append(value) or 0,
            )
            == 0
        )
    assert calls[-1]["status_detail"] == "already_published"

    foreign = dict(remote, author={"login": "other"})
    with (
        patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)),
        patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")),
        patch.object(executor, "_list_issue_comments", return_value=([foreign], "")),
        patch.object(executor, "_post_gh_comment") as post,
    ):
        assert (
            executor._run_human_history_comment_publish(
                args, data, "/bin/gh", lambda *a, **k: 1, lambda _: 0
            )
            == 1
        )
        post.assert_not_called()


def test_foreign_different_identity_marker_blocks_create_before_any_mutation():
    marker = _marker("owned identity")
    foreign_marker = _marker("foreign identity")
    args = SimpleNamespace(
        issue_number=1908,
        repo=TRUSTED_REPO,
        command_id="issue_comment.publish",
        dry_run=False,
    )
    foreign_different_identity = {
        "body": _body("別の有効な履歴", marker=foreign_marker),
        "url": "https://github.com/x/y/issues/1#issuecomment-43",
        "id": "foreign",
        "author": {"login": "other"},
    }
    failures: list[str] = []
    with (
        patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)),
        patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")),
        patch.object(
            executor, "_list_issue_comments", return_value=([foreign_different_identity], "")
        ),
        patch.object(executor, "_post_gh_comment") as post,
        patch.object(executor, "_patch_gh_comment") as patch_comment,
        patch.object(executor, "_human_history_readback") as readback,
    ):
        assert (
            executor._run_human_history_comment_publish(
                args,
                {"comment_body": _body(marker=marker), "marker": marker},
                "/bin/gh",
                lambda reason, **_kwargs: failures.append(reason) or 1,
                lambda _value: 0,
            )
            == 1
        )
    assert failures == ["human_history_remote_marker_author_mismatch"]
    post.assert_not_called()
    patch_comment.assert_not_called()
    readback.assert_not_called()


def test_marker_source_vectors_and_diagnostic_failures_are_non_mutating():
    marker = _marker("strict-vectors")
    canonical = _body("é", marker)
    nfd = _body("é", marker)
    parsed_nfc, nfc_error = executor._parse_human_history_marker_source(canonical)
    parsed_nfd, nfd_error = executor._parse_human_history_marker_source(nfd)
    assert nfc_error == nfd_error == ""
    assert parsed_nfc and parsed_nfd
    assert (
        parsed_nfc["content_digest"]
        == "4a99557e4033c3539de2eb65472017cad5f9557f7a0625a09f1c3f6e2ba69c4c"
    )
    assert (
        parsed_nfd["content_digest"]
        == "bf12767b0f2a56b2190075bae8169f656e3ce8d6357d4aff184bc6c7ea48f9f6"
    )
    assert parsed_nfc["content_digest"] != parsed_nfd["content_digest"]

    malformed = {
        "missing_delimiter": "prefix loop-protocol/human-history:v1:sha256:" + "a" * 64,
        "version_mismatch": _body(
            marker="<!-- loop-protocol/human-history:v2:sha256:" + "a" * 64 + " -->"
        ),
        "missing_digest": _body(marker="<!-- loop-protocol/human-history:v1:sha256: -->"),
        "uppercase_digest": _body(
            marker="<!-- loop-protocol/human-history:v1:sha256:" + "A" * 64 + " -->"
        ),
        "extra_attribute": _body(marker=marker[:-3] + " extra -->"),
        "leading_whitespace": _body(marker=" " + marker),
        "trailing_whitespace": _body(marker=marker + " "),
        "inline": "visible " + marker + "\n",
        "nonfinal": _body(marker=marker) + "later\n",
        "multiline": "visible\n<!-- loop-protocol/human-history:v1:sha256:"
        + "a" * 32
        + "\n"
        + "a" * 32
        + " -->\n",
        "multiple": _body(marker=marker) + marker,
    }
    for name, body in malformed.items():
        parsed, error = executor._parse_human_history_marker_source(body)
        assert parsed is None, name
        assert error, name

    # All terminal newline forms retain the same byte-level visible content.
    for body in (canonical, canonical[:-1], canonical + "\n\n", canonical.replace("\n", "\r\n")):
        parsed, error = executor._parse_human_history_marker_source(body)
        assert error == "" and parsed
        assert parsed["content_digest"] == parsed_nfc["content_digest"]

    args = SimpleNamespace(
        issue_number=1908,
        repo=TRUSTED_REPO,
        command_id="issue_comment.publish",
        dry_run=False,
    )
    for name, body in malformed.items():
        failures: list[str] = []
        with (
            patch.object(executor, "_post_gh_comment") as post,
            patch.object(executor, "_patch_gh_comment") as patch_comment,
            patch.object(executor, "_human_history_readback") as readback,
        ):
            assert (
                executor._run_human_history_comment_publish(
                    args,
                    {"comment_body": body, "marker": marker},
                    "/bin/gh",
                    lambda reason, **_kwargs: failures.append(reason) or 1,  # noqa: B023
                    lambda _value: 0,
                )
                == 1
            )
        expected_error = executor._parse_human_history_marker_source(body)[1]
        assert failures == ["human_history_marker_diagnostic_failure:" + expected_error], name
        post.assert_not_called()
        patch_comment.assert_not_called()
        readback.assert_not_called()
