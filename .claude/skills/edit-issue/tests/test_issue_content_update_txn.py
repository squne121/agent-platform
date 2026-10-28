"""Transaction ordering regression coverage for Issue #1660."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import edit_issue_txn as txn  # noqa: E402


def test_comment_failure_after_content_update_is_failed_after_mutation(monkeypatch):
    body = "new body"
    input_data = {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 1660,
        "repo": "squne121/agent-platform",
        "new_body_file": "tmp/ignored.md",
        "readiness_forwarding_payload": {"readiness_result": {
            "status": "go", "body_sha256": "sha256:x", "source_checks": [],
            "errors": [], "readiness_result_ref": "artifacts/ready.json",
        }},
        "comment_mode": {"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
        "expected_previous_body_sha256": txn._sha256_text("old body"),
        "expected_previous_updated_at": "2026-07-22T00:00:00Z",
        "title_update": {"required": True, "proposed_title": "new title", "reason": "scope reframe"},
    }
    readbacks = iter([
        {"title": "old title", "body": "old body", "updatedAt": "2026-07-22T00:00:00Z"},
        {"title": "new title", "body": body, "updatedAt": "2026-07-22T00:00:01Z"},
    ])
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_read_text_file", lambda _: body)
    monkeypatch.setattr(txn, "_run_command", lambda *_: subprocess.CompletedProcess([], 0, "", ""))
    metadata_input_path = (
        "artifacts/1660/issue-metadata/issue_content.update/input.json"
    )
    monkeypatch.setattr(
        txn, "_write_issue_metadata_input", lambda *_: metadata_input_path
    )
    monkeypatch.setattr(txn, "_invoke_controlled_exec", lambda command, *_: (
        subprocess.CompletedProcess([], 0, "{}", ""), {"new_body_sha256": txn._sha256_text(body)}
    ) if command == "issue_content.update" else (subprocess.CompletedProcess([], 1, "", "comment failed"), None))

    result = txn.run_transaction(input_data)

    assert result["status"] == "failed_after_mutation"
    assert result["mutation_started"] is True
    assert result["body_update"]["status"] == "ok"
    assert result["comment_publish"]["status"] == "failed"


def _transaction_input(current_body: str, *, title: str = "new title") -> dict:
    return {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 1660,
        "repo": "squne121/agent-platform",
        "new_body_file": "tmp/ignored.md",
        "readiness_forwarding_payload": {"readiness_result": {
            "status": "go", "body_sha256": "sha256:x", "source_checks": [],
            "errors": [], "readiness_result_ref": "artifacts/ready.json",
        }},
        "comment_mode": {"mode": "skip"},
        "expected_previous_body_sha256": txn._sha256_text(current_body),
        "expected_previous_updated_at": "2026-07-22T00:00:00Z",
        "title_update": {"required": True, "proposed_title": title, "reason": "scope reframe"},
    }


def test_title_only_preserves_noncanonical_current_body_byte_for_byte(monkeypatch):
    current_body = "legacy body without canonical hygiene\n"
    input_data = _transaction_input(current_body)
    readbacks = iter([
        {"title": "old title", "body": current_body, "updatedAt": "2026-07-22T00:00:00Z"},
        {"title": "new title", "body": current_body, "updatedAt": "2026-07-22T00:00:01Z"},
    ])
    captured: dict = {}
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_read_text_file", lambda _: current_body)
    monkeypatch.setattr(txn, "_run_command", lambda *_: (_ for _ in ()).throw(AssertionError("hygiene must not run")))
    monkeypatch.setattr(
        txn,
        "_write_issue_metadata_input",
        lambda _issue, _command, payload: captured.update(payload) or "artifacts/input.json",
    )
    monkeypatch.setattr(txn, "_invoke_controlled_exec", lambda *_: (
        subprocess.CompletedProcess([], 0, "{}", ""), {"new_body_sha256": txn._sha256_text(current_body)}
    ))

    result = txn.run_transaction(input_data)

    assert result["status"] == "ok"
    assert captured["new_body"] == current_body
    assert captured["new_body_sha256"] == txn._sha256_text(current_body)
    assert result["content_update"] == {
        "previous_title": "old title",
        "requested_title": "new title",
        "remote_current_title": "new title",
        "patch_attempted": True,
        "mutation_outcome": "applied",
    }


def test_post_hygiene_candidate_equal_to_current_returns_no_change(monkeypatch):
    current_body = "canonical body"
    input_data = _transaction_input(current_body, title="old title")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        txn, "_fetch_issue", lambda *_: (
            {"title": "old title", "body": current_body, "updatedAt": "2026-07-22T00:00:00Z"}, ""
        )
    )
    monkeypatch.setattr(txn, "_read_text_file", lambda _: "noncanonical candidate")

    def fake_run(args):
        commands.append(args)
        if str(txn.HYGIENE_SCRIPT) in args:
            candidate = Path(args[args.index("--out-file") + 1])
            candidate.write_text(current_body, encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(txn, "_run_command", fake_run)
    monkeypatch.setattr(
        txn,
        "_invoke_controlled_exec",
        lambda *_: (_ for _ in ()).throw(AssertionError("executor must not run")),
    )

    result = txn.run_transaction(input_data)

    assert result["status"] == "no_change"
    assert result["body_update"]["attempted"] is False
    assert result["content_update"]["mutation_outcome"] == "no_change"
    assert any(str(txn.HYGIENE_SCRIPT) in args for args in commands)
