from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import edit_issue_txn as txn  # noqa: E402


class _CP:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def repo_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tmp").mkdir()
    (root / "artifacts").mkdir()
    monkeypatch.setattr(txn, "REPO_ROOT", root)
    monkeypatch.setattr(txn, "CONTROLLED_EXEC", root / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py")
    monkeypatch.setattr(txn, "GUARD_SCRIPT", root / "guard.py")
    monkeypatch.setattr(txn, "HYGIENE_SCRIPT", root / "hygiene.py")
    monkeypatch.setattr(txn, "READINESS_SCRIPT", root / "readiness.py")
    return root


def _minimal_input(repo_tmp: Path, *, comment_mode: dict | None = None, title_required: bool = False) -> dict:
    new_body = repo_tmp / "tmp" / "new_body.md"
    new_body.write_text("new issue body", encoding="utf-8")
    return {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 1287,
        "repo": "squne121/agent-platform",
        "new_body_file": "tmp/new_body.md",
        "readiness_forwarding_payload": {
            "readiness_result": {
                "status": "go",
                "body_sha256": "sha256:old",
                "source_checks": ["contract_readiness_check.py --mode static"],
                "errors": [],
                "readiness_result_ref": "artifact.json",
            }
        },
        "comment_mode": comment_mode or {"mode": "skip"},
        "expected_previous_body_sha256": txn._sha256_text("old issue body"),
        "expected_previous_updated_at": "2026-07-03T10:40:51Z",
        "title_update": {
            "required": title_required,
            "proposed_title": "x" if title_required else None,
            "reason": "x" if title_required else None,
        },
    }


def _normal_input_with_new_body(repo_tmp: Path, new_body: str) -> dict:
    (repo_tmp / "tmp" / "new_body.md").write_text(new_body, encoding="utf-8")
    payload = _minimal_input(repo_tmp)
    return payload


def test_schema_contracts_are_closed() -> None:
    docs = (
        Path(__file__).resolve().parents[4] / "docs" / "dev" / "agent-skill-boundaries.md"
    ).read_text(encoding="utf-8")
    assert "### ISSUE_EDIT_TXN_INPUT_V1" in docs
    assert "### ISSUE_EDIT_TXN_RESULT_V1" in docs
    assert docs.count("additionalProperties: false") >= 2
    assert "body_update:" in docs
    assert "comment_publish:" in docs
    # Issue #2316: canonical schema must declare both additive properties
    # (not just the heading/additionalProperties count) so the Python
    # validator (TOP_LEVEL_KEYS) and the canonical schema never split-brain.
    input_section = docs.split("### ISSUE_EDIT_TXN_INPUT_V1", 1)[1].split("### ISSUE_EDIT_TXN_RESULT_V1", 1)[0]
    assert "  rewrite_lane:" in input_section
    assert "  semantic_rewrite_constraints:" in input_section


def _semantic_producer_shaped_constraints() -> dict:
    # Mirrors the exact shape join_review_results.py's
    # _semantic_rewrite_constraints() emits (scripts/issue-refinement-loop,
    # lines ~132-145), including the fields it forwards from
    # _result(rewrite_lane="semantic", ...) at lines ~226-237.
    return {
        "schema_version": "SEMANTIC_REWRITE_CONSTRAINTS_V1",
        "source_artifact": "artifacts/2296/semantic-review/2026-08-01T00-00-00Z.json",
        # deterministic placeholder with the real sha256 hex digest shape:
        # a 64-character lowercase hex string, matching the constraint
        # ^[0-9a-f]{64}$ from schemas/semantic_review_result_v1.schema.json.
        "checked_body_sha256": "a" * 64,
        # SEMANTIC_REVIEW_RESULT_V1 findings entries are additionalProperties:
        # false with only severity/summary required (plus optional
        # evidence_refs / recommended_fix / requires_owner_choice /
        # owner_disposition) -- there is no "id" field in the real producer
        # shape, so it is omitted here.
        "findings": [
            {
                "severity": "blocker",
                "summary": "AC の VC が Allowed Paths 外を参照している",
            }
        ],
        "max_rewrite_attempts": 2,
        # Issue #2316 fix_delta (P2-1): matches
        # join_review_results.DEFAULT_NO_PROGRESS_ROUTE exactly.
        "no_progress_route": "human_judgment_required",
    }


def _extract_issue_edit_txn_input_schema() -> dict:
    # Issue #2316 fix_delta (P1-2): reuse the same doc-slicing approach as
    # test_schema_contracts_are_closed above (locate the section between the
    # ISSUE_EDIT_TXN_INPUT_V1 and ISSUE_EDIT_TXN_RESULT_V1 headings, then pull
    # out the fenced ```yaml block) so the extraction logic never split-brains
    # between the two tests.
    docs = (
        Path(__file__).resolve().parents[4] / "docs" / "dev" / "agent-skill-boundaries.md"
    ).read_text(encoding="utf-8")
    input_section = docs.split("### ISSUE_EDIT_TXN_INPUT_V1", 1)[1].split("### ISSUE_EDIT_TXN_RESULT_V1", 1)[0]
    block = input_section.split("```yaml", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)


def _base_schema_instance() -> dict:
    # Minimal but otherwise-valid ISSUE_EDIT_TXN_INPUT_V1 instance (reuses the
    # same shape as _minimal_input(), without the repo_tmp-bound file paths
    # that only matter for the executable validator, not the JSON Schema).
    return {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 1287,
        "repo": "squne121/agent-platform",
        "new_body_file": "tmp/new_body.md",
        "readiness_forwarding_payload": {
            "readiness_result": {
                "status": "go",
                "body_sha256": "sha256:old",
                "source_checks": ["contract_readiness_check.py --mode static"],
                "errors": [],
                "readiness_result_ref": "artifact.json",
            }
        },
        "comment_mode": {"mode": "skip"},
        "expected_previous_body_sha256": "0" * 64,
        "expected_previous_updated_at": "2026-07-03T10:40:51Z",
        "title_update": {"required": False, "proposed_title": None, "reason": None},
    }


@pytest.mark.parametrize(
    ("rewrite_lane", "constraints", "expect_valid"),
    [
        pytest.param(None, "omit", True, id="legacy_both_fields_omitted"),
        pytest.param("semantic", _semantic_producer_shaped_constraints(), True, id="semantic_plus_object"),
        pytest.param("semantic", "omit", False, id="semantic_plus_missing_constraints_key"),
        pytest.param("semantic", None, False, id="semantic_plus_null_constraints"),
        pytest.param(
            None, _semantic_producer_shaped_constraints(), False, id="omitted_lane_plus_constraints_object"
        ),
        pytest.param(None, None, False, id="omitted_lane_plus_constraints_null"),
        pytest.param(
            "fail_closed_repair",
            _semantic_producer_shaped_constraints(),
            False,
            id="fail_closed_repair_lane_plus_constraints_object",
        ),
        pytest.param("fail_closed_repair", None, False, id="fail_closed_repair_lane_plus_constraints_null"),
        pytest.param("bogus", "omit", False, id="invalid_lane_value_plus_no_constraints"),
    ],
)
def test_issue_edit_txn_input_schema_presence_correlation(
    rewrite_lane: "str | None", constraints: object, expect_valid: bool
) -> None:
    # Issue #2316 fix_delta (P1-2): validate the canonical
    # ISSUE_EDIT_TXN_INPUT_V1 JSON Schema block itself (not just token
    # strings) against the full presence-correlation state matrix from the
    # human REQUEST_CHANGES review, using the real jsonschema library so a
    # regression in the doc's allOf/if/then/else block is actually caught.
    schema = _extract_issue_edit_txn_input_schema()
    instance = _base_schema_instance()
    if rewrite_lane is not None:
        instance["rewrite_lane"] = rewrite_lane
    if constraints != "omit":
        instance["semantic_rewrite_constraints"] = constraints

    if expect_valid:
        jsonschema.validate(instance, schema)
    else:
        with pytest.raises(jsonschema.exceptions.ValidationError):
            jsonschema.validate(instance, schema)


def test_rewrite_lane_omitted_legacy_payload_still_accepted(repo_tmp: Path) -> None:
    # GIVEN a legacy input omitting both rewrite_lane and
    # semantic_rewrite_constraints (pre-#2316 shape)
    payload = _minimal_input(repo_tmp)
    assert "rewrite_lane" not in payload
    assert "semantic_rewrite_constraints" not in payload
    # WHEN validated
    # THEN it is accepted unchanged (AC2 -- no regression in fail_closed_repair lane)
    txn._validate_input_payload(payload)


def test_rewrite_lane_fail_closed_repair_explicit_without_constraints_accepted(repo_tmp: Path) -> None:
    # GIVEN an explicit fail_closed_repair lane with no constraints
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "fail_closed_repair"
    # WHEN validated THEN it is accepted
    txn._validate_input_payload(payload)


def test_rewrite_lane_semantic_with_producer_shaped_constraints_accepted(repo_tmp: Path) -> None:
    # GIVEN a rewrite_lane=semantic input bound to the real producer
    # (join_review_results.py) shape (AC3)
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "semantic"
    constraints = _semantic_producer_shaped_constraints()
    payload["semantic_rewrite_constraints"] = constraints
    # AND the fixture's checked_body_sha256 matches the real producer shape:
    # a 64-character lowercase hex digest, per
    # schemas/semantic_review_result_v1.schema.json (^[0-9a-f]{64}$) (Issue #2329)
    digest = constraints["checked_body_sha256"]
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    # WHEN validated THEN it is accepted without input_unknown_keys
    txn._validate_input_payload(payload)


def test_invalid_rewrite_lane_rejected(repo_tmp: Path) -> None:
    # GIVEN a rewrite_lane outside the enum (AC4)
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "invalid_lane"
    # WHEN validated THEN it is fail-closed rejected with a clear reason code
    with pytest.raises(ValueError, match="rewrite_lane_invalid"):
        txn._validate_input_payload(payload)


def test_semantic_rewrite_lane_without_constraints_rejected(repo_tmp: Path) -> None:
    # GIVEN rewrite_lane=semantic but semantic_rewrite_constraints omitted (AC5)
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "semantic"
    # WHEN validated THEN it is fail-closed rejected
    with pytest.raises(ValueError, match="semantic_rewrite_constraints_required_for_semantic_lane"):
        txn._validate_input_payload(payload)


def test_semantic_rewrite_constraints_without_semantic_rewrite_lane_rejected(repo_tmp: Path) -> None:
    # GIVEN semantic_rewrite_constraints present but rewrite_lane omitted
    # (defaults to fail_closed_repair) (AC6)
    payload = _minimal_input(repo_tmp)
    payload["semantic_rewrite_constraints"] = _semantic_producer_shaped_constraints()
    # WHEN validated THEN it is fail-closed rejected
    with pytest.raises(ValueError, match="semantic_rewrite_constraints_forbidden_without_semantic_lane"):
        txn._validate_input_payload(payload)


def test_semantic_rewrite_constraints_with_fail_closed_repair_lane_rejected(repo_tmp: Path) -> None:
    # GIVEN semantic_rewrite_constraints present with an explicit
    # fail_closed_repair lane (AC6, non-omitted variant)
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "fail_closed_repair"
    payload["semantic_rewrite_constraints"] = _semantic_producer_shaped_constraints()
    with pytest.raises(ValueError, match="semantic_rewrite_constraints_forbidden_without_semantic_lane"):
        txn._validate_input_payload(payload)


def test_semantic_rewrite_constraints_wrong_schema_version_missing_rejected(repo_tmp: Path) -> None:
    # GIVEN semantic_rewrite_constraints missing schema_version (AC7)
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "semantic"
    constraints = _semantic_producer_shaped_constraints()
    del constraints["schema_version"]
    payload["semantic_rewrite_constraints"] = constraints
    with pytest.raises(ValueError, match="semantic_rewrite_constraints_schema_version_invalid"):
        txn._validate_input_payload(payload)


def test_semantic_rewrite_constraints_wrong_schema_version_mismatched_rejected(repo_tmp: Path) -> None:
    # GIVEN semantic_rewrite_constraints with a mismatched schema_version (AC7)
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "semantic"
    constraints = _semantic_producer_shaped_constraints()
    constraints["schema_version"] = "SEMANTIC_REWRITE_CONSTRAINTS_V2"
    payload["semantic_rewrite_constraints"] = constraints
    with pytest.raises(ValueError, match="semantic_rewrite_constraints_schema_version_invalid"):
        txn._validate_input_payload(payload)


def test_semantic_rewrite_constraints_not_object_rejected(repo_tmp: Path) -> None:
    # GIVEN semantic_rewrite_constraints that is not an object (AC7)
    payload = _minimal_input(repo_tmp)
    payload["rewrite_lane"] = "semantic"
    payload["semantic_rewrite_constraints"] = "not-an-object"
    with pytest.raises(ValueError, match="semantic_rewrite_constraints_invalid"):
        txn._validate_input_payload(payload)


def test_input_unknown_keys_still_rejected(repo_tmp: Path) -> None:
    # GIVEN a top-level key outside TOP_LEVEL_KEYS, including the two new
    # additive keys added by Issue #2316 (AC8 -- _require_closed_keys
    # fail-closed behaviour is preserved)
    payload = _minimal_input(repo_tmp)
    payload["totally_unknown_key"] = True
    with pytest.raises(ValueError, match="input_unknown_keys"):
        txn._validate_input_payload(payload)


def test_no_raw_issue_mutation_or_shell_escape_in_production_path() -> None:
    source = (SCRIPTS_DIR / "edit_issue_txn.py").read_text(encoding="utf-8")
    forbidden = [
        "gh issue edit",
        "gh issue comment",
        "gh api --method PATCH",
        "gh api --method POST",
        "shell=True",
        "bash -c",
        "sh -c",
        "python -c",
    ]
    for token in forbidden:
        assert token not in source
    assert "issue_content.update" in source
    assert "issue_comment.publish" in source


def test_title_update_routes_through_content_executor(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readbacks = iter([
        {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
        {"title": "x", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
    ])
    calls: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return next(readbacks), ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args or str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        calls.append(command_id)
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    result = txn.run_transaction(_minimal_input(repo_tmp, title_required=True))
    assert result["status"] == "ok"
    assert result["mutation_started"] is True
    assert calls == ["issue_content.update"]


@pytest.mark.parametrize(
    ("variant", "guard_rc", "readiness_rc"),
    [
        ("stale", 0, 0),
        ("guard", 2, 0),
        ("readiness", 0, 1),
    ],
)
def test_no_mutation_before_guard_readiness_or_stale_precondition(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    guard_rc: int,
    readiness_rc: int,
) -> None:
    events: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        body = "old issue body" if variant != "stale" else "different body"
        return {"title": "old", "body": body, "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            events.append("guard")
            return _CP(guard_rc, stderr="guard failed")
        if str(txn.HYGIENE_SCRIPT) in args:
            events.append("hygiene")
            return _CP(1)
        if str(txn.READINESS_SCRIPT) in args:
            events.append("readiness")
            return _CP(readiness_rc, stderr="readiness failed")
        pytest.fail(f"unexpected command: {args}")

    def _invoke(*_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        pytest.fail("controlled executor must not be invoked")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_minimal_input(repo_tmp))
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["body_update"]["attempted"] is False
    assert result["body_update"]["artifact_ref"] is None
    if variant == "stale":
        assert events == []
    elif variant == "guard":
        assert events == ["guard"]
    else:
        assert events == ["guard", "hygiene", "readiness"]


def test_1844_parent_candidate_runs_real_local_validators_before_executor(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real guard, hygiene, and readiness reject/accept before remote mutation."""
    parent_body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: parent readiness integration
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary

Parent summary.

## Goal

Keep parent readiness valid.

## Desired Destination

Validated parent mutation.

## Current Validated Scope

Readiness consumer integration.

## Decisions Fixed

- 2026-07-30: use the existing issue readiness profile.

## Quality Decision Record

- Status: N/A

## Parent Closure Rule

- Close after child completion.

## Child Issues

- [ ] #1

## Remaining Parent Gaps

- [ ] none

## Phase Handoff Contract

- Parent handoff remains explicit.

## Acceptance Criteria

- [ ] AC1: real local validators run before remote mutation.
"""
    payload = _minimal_input(repo_tmp)
    (repo_tmp / "tmp" / "new_body.md").write_text(parent_body, encoding="utf-8")
    payload["expected_previous_body_sha256"] = txn._sha256_text("old issue body")

    production_root = Path(__file__).resolve().parents[4]
    monkeypatch.setattr(
        txn,
        "GUARD_SCRIPT",
        production_root / ".claude/skills/edit-issue/scripts/guard-issue-body.py",
    )
    monkeypatch.setattr(
        txn,
        "HYGIENE_SCRIPT",
        production_root / ".claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py",
    )
    monkeypatch.setattr(
        txn,
        "READINESS_SCRIPT",
        production_root / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
    )

    readbacks = iter([
        {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
        {"title": "old", "body": parent_body, "updatedAt": "2026-07-03T10:41:51Z"},
    ])
    invoked: list[str] = []

    monkeypatch.setattr(txn, "_fetch_issue", lambda *_args, **_kwargs: (next(readbacks), ""))

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        invoked.append(command_id)
        return _CP(0, stdout=json.dumps({"new_body_sha256": txn._sha256_text(parent_body)})), {
            "new_body_sha256": txn._sha256_text(parent_body)
        }

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    result = txn.run_transaction(payload)

    assert result["status"] == "ok"
    assert result["mutation_started"] is True
    assert invoked == ["issue_content.update"]


def test_controlled_executor_invoked_with_json_and_parsed(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        calls.append(["fetch"])
        fetch_calls["count"] += 1
        if fetch_calls["count"] > 1:
            return {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"}, ""
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            calls.append(args)
            return _CP(0, stdout='{"new_body_sha256":"sha256:parsed"}')
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    result = txn.run_transaction(_normal_input_with_new_body(repo_tmp, "new issue body"))
    assert any(arg == "--json" for call in calls for arg in call)
    assert result["status"] == "ok"
    assert result["body_update"]["new_body_sha256"] == "sha256:parsed"


def test_comment_publish_success_propagates_comment_id_url_body_sha(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_body = repo_tmp / "tmp" / "comment.md"
    comment_body.write_text("comment body <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    calls: list[str] = []
    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""
        return {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            if "issue_content.update" in args:
                calls.append("issue_content.update")
                return _CP(0, stdout='{"new_body_sha256":"sha256:new"}')
            if "issue_comment.publish" in args:
                calls.append("issue_comment.publish")
                return _CP(
                    0,
                    stdout=json.dumps(
                        {
                            "comment_id": "c-123",
                            "comment_url": "https://github.com/squne121/agent-platform/issues/1287#issuecomment-123",
                            "body_sha256": "sha256:comment",
                        }
                    ),
                )
            return _CP(0, stdout="{}")
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    result = txn.run_transaction(payload)
    assert result["status"] == "ok"
    assert calls == ["issue_content.update", "issue_comment.publish"]
    assert result["comment_publish"]["comment_id"] == "c-123"
    assert result["comment_publish"]["comment_url"] == "https://github.com/squne121/agent-platform/issues/1287#issuecomment-123"
    assert result["comment_publish"]["comment_body_sha256"] == "sha256:comment"


def test_body_unchanged_comment_publish_skips_body_update(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_body = repo_tmp / "tmp" / "comment.md"
    comment_body.write_text("publish marker", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "publish marker"},
    )
    (repo_tmp / "tmp" / "new_body.md").write_text("old issue body", encoding="utf-8")
    payload["expected_previous_body_sha256"] = txn._sha256_text("old issue body")

    called: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        called.append(command_id)
        if command_id == "issue_comment.publish":
            return (
                _CP(
                    0,
                    stdout=json.dumps(
                        {
                            "comment_id": "c-2",
                            "comment_url": "https://example.com/c2",
                            "body_sha256": "sha256:comment",
                        }
                    ),
                ),
                {
                    "comment_id": "c-2",
                    "comment_url": "https://example.com/c2",
                    "body_sha256": "sha256:comment",
                },
            )
        pytest.fail(command_id)

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "ok"
    assert result["body_update"]["attempted"] is False
    assert result["comment_publish"]["attempted"] is True
    assert result["comment_publish"]["status"] == "ok"
    assert result["comment_publish"]["comment_id"] == "c-2"
    assert called == ["issue_comment.publish"]


def test_safe_repo_file_rejects_symlink_component_for_input_new_body_comment(repo_tmp: Path) -> None:
    real_dir = repo_tmp / "tmp" / "real"
    real_dir.mkdir()
    (real_dir / "candidate.md").write_text("x", encoding="utf-8")

    link_dir = repo_tmp / "tmp" / "link"
    link_dir.symlink_to(real_dir)

    with pytest.raises(ValueError, match="symlink_not_allowed"):
        txn._safe_repo_file("tmp/link/candidate.md")


def test_safe_repo_file_rejects_repo_prefix_collision(repo_tmp: Path) -> None:
    sibling = repo_tmp.parent / f"{repo_tmp.name}-outside"
    sibling.mkdir()
    candidate = sibling / "outside.md"
    candidate.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="path_not_found|path_must_not_escape_repo"):
        txn._safe_repo_file(f"../{sibling.name}/outside.md")


def test_body_update_success_comment_or_readback_failure_maps_failed_after_mutation(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_body = repo_tmp / "tmp" / "comment.md"
    comment_body.write_text("comment body <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""
        return {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_content.update":
            return _CP(0, stdout='{"new_body_sha256":"sha256:body"}'), {"new_body_sha256": "sha256:body"}
        if command_id == "issue_comment.publish":
            return _CP(1, stderr="child stderr with secret"), None
        pytest.fail(command_id)

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    result = txn.run_transaction(payload)
    assert result["status"] == "failed_after_mutation"
    assert result["mutation_started"] is True
    assert result["body_update"]["attempted"] is True
    assert result["comment_publish"]["attempted"] is True
    assert result["comment_publish"]["status"] == "failed"


def test_child_timeout_maps_to_single_bounded_json(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_json = repo_tmp / "tmp" / "input.json"
    input_json.write_text(json.dumps(_normal_input_with_new_body(repo_tmp, "new issue body")), encoding="utf-8")

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            return _CP(124, stderr="child command timeout after 30s")
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    rc = txn.main(["--input-file", "tmp/input.json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 1
    assert parsed["status"] == "mutation_outcome_unknown"
    assert parsed["content_update"]["patch_attempted"] is True
    assert parsed["content_update"]["mutation_outcome"] == "unknown"
    assert len(out.splitlines()) == 1
    assert len(parsed["errors"]) == 1
    assert len(parsed["errors"][0]["message"]) <= 240


def test_needs_fix_forwarding_does_not_mutate_without_resolution_evidence(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _minimal_input(repo_tmp)
    payload["readiness_forwarding_payload"]["readiness_result"]["status"] = "needs_fix"
    payload["readiness_forwarding_payload"]["readiness_result"].pop("resolution_evidence", None)

    def _run(*_args: object, **_kwargs: object) -> _CP:
        pytest.fail("child subprocess should not be invoked")

    # Issue #2922: the rejection is deferred until after the live readback
    # (`_fetch_issue`); an OPEN Issue is still rejected with no child process.
    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {
            "title": "old",
            "body": "old issue body",
            "updatedAt": "2026-07-03T10:40:51Z",
            "state": "OPEN",
        }, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["body_update"]["attempted"] is False
    assert result["errors"][0]["code"] == "readiness_needs_fix_without_resolution_evidence"


def test_fetch_issue_argv_single_snapshot_includes_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Issue #2922: one `gh issue view` readback carries title / body /
    # updatedAt / state; there is no separate state-only request helper.
    seen: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> _CP:
        seen.append(args)
        return _CP(0, stdout='{"title":"t","body":"b","updatedAt":"u","state":"CLOSED"}')

    monkeypatch.setattr(txn, "_run_command", _run)
    issue, err = txn._fetch_issue(1, "squne121/agent-platform")
    assert err == ""
    assert issue == {"title": "t", "body": "b", "updatedAt": "u", "state": "CLOSED"}
    assert len(seen) == 1
    assert seen[0][1:] == [
        "issue", "view", "1", "--repo", "squne121/agent-platform", "--json", "title,body,updatedAt,state",
    ]
    assert not hasattr(txn, "_fetch_issue_state")


def _assert_no_child_stdout_stderr_leak(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (repo_tmp / "tmp" / "new_body.md").write_text("old issue body", encoding="utf-8")
    (repo_tmp / "tmp" / "input.json").write_text(json.dumps(_minimal_input(repo_tmp)), encoding="utf-8")
    secret = "very secret child stdout " * 30

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(1, stdout=secret)
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    rc = txn.main(["--input-file", "tmp/input.json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 1
    assert parsed["status"] == "failed_no_mutation"
    assert len(out.splitlines()) == 1
    assert secret not in out
    assert parsed["schema"] == txn.RESULT_SCHEMA
    assert parsed["errors"][0]["message"] != secret
    assert len(parsed["errors"][0]["message"]) <= 240


def test_stdout_leak_real_child_stdout_stderr_not_mocked(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_no_child_stdout_stderr_leak(repo_tmp, monkeypatch, capsys)


@pytest.mark.parametrize(
    "_case_name",
    ["stdout_single_bounded_json_no_body_or_child_output_leak"],
    ids=["stdout_single_bounded_json_no_body_or_child_output_leak"],
)
def test_child_output_leak_bounded_json_path(
    _case_name: str,
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_no_child_stdout_stderr_leak(repo_tmp, monkeypatch, capsys)


def test_executor_inputs_under_issue_metadata_namespace(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_tmp / "tmp" / "comment.md").write_text("comment <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    calls: list[str] = []

    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        fetch_calls["count"] += 1
        if fetch_calls["count"] > 1:
            return {
                "title": "old",
                "body": "new issue body",
                "updatedAt": "2026-07-03T10:41:51Z",
            }, ""
        return {
            "title": "old",
            "body": "old issue body",
            "updatedAt": "2026-07-03T10:40:51Z",
        }, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            if "issue_body.update" in args:
                return _CP(0, stdout='{"new_body_sha256":"sha256:x"}'), {
                    "new_body_sha256": "sha256:x"
                }
            if "issue_comment.publish" in args:
                return (
                    _CP(0, stdout='{"comment_id":"c-1","comment_url":"https://example.com/c1","body_sha256":"sha256:c"}'),
                    {
                        "comment_id": "c-1",
                        "comment_url": "https://example.com/c1",
                        "body_sha256": "sha256:c",
                    },
                )
            pytest.fail("unknown command")
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, issue_number: int, repo: str, input_ref: str) -> tuple[_CP, dict | None]:
        calls.append(input_ref)
        assert input_ref.startswith(f"artifacts/{issue_number}/issue-metadata/{command_id}/")
        if command_id == "issue_body.update":
            return _CP(0, stdout='{"new_body_sha256":"sha256:x"}'), {"new_body_sha256": "sha256:x"}
        return _CP(0, stdout='{"comment_id":"c-1","comment_url":"https://example.com/c1","body_sha256":"sha256:c"}'), {
            "comment_id": "c-1",
            "comment_url": "https://example.com/c1",
            "body_sha256": "sha256:c",
        }

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "ok"
    assert len(calls) == 2


def test_skill_and_issue_editor_no_raw_existing_issue_mutation_contract() -> None:
    """Issue #1734: issue-author was split into issue-creator/issue-editor;
    existing-Issue mutation now lives in issue-editor.md.
    """
    skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    agent = (Path(__file__).resolve().parents[3] / "agents" / "issue-editor.md").read_text(encoding="utf-8")
    forbidden = ["gh issue edit", "gh issue comment", "gh api --method PATCH", "gh api --method POST"]
    for token in forbidden:
        assert token not in skill
        assert token not in agent
    assert "edit_issue_txn.py" in skill
    assert "edit_issue_txn.py" in agent


def test_dependency_policy_separates_txn_helper_from_end_to_end_raw_removal() -> None:
    skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    assert "required_for_txn_helper" in skill
    assert "required_for_end_to_end_raw_mutation_removal" in skill
    assert "#1284 / PR #1295" in skill
    assert "#1291 / PR #1298" in skill


# ---------------------------------------------------------------------------
# Issue #1883: native relationship (parent / blockedBy / blocking) sync
# ---------------------------------------------------------------------------


def _minimal_input_with_relationships(repo_tmp: Path, native_relationships: dict) -> dict:
    payload = _minimal_input(repo_tmp)
    payload["native_relationships"] = native_relationships
    return payload


def _base_relationships(**overrides: object) -> dict:
    base = {
        "expected_before": {"parent": None, "blocked_by": [], "blocking": []},
        "parent": {"action": "unchanged", "issue_number": None},
        "add_blocked_by": [],
        "remove_blocked_by": [],
        "add_blocking": [],
        "remove_blocking": [],
    }
    base.update(overrides)
    return base


def _stub_content_flow(monkeypatch: pytest.MonkeyPatch, repo_tmp: Path) -> None:
    """Stub the pre-existing (pre-#1883) content mutation pipeline so tests
    can focus on the native relationship gate without needing real guard/
    hygiene/readiness scripts."""

    def _run(args: list[str]) -> _CP:
        if str(repo_tmp / "guard.py") in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(repo_tmp / "hygiene.py") in args or str(repo_tmp / "readiness.py") in args:
            return _CP(0)
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_run_command", _run)


def test_preflight_failure_blocks_all_mutation(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: graph invariant preflight failure blocks native AND content mutation."""
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(parent={"action": "set", "issue_number": 1287}),  # self-parent (issue_number=1287)
    )

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("no mutation of any kind must be attempted")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["native_relationships"]["attempted"] is False
    assert result["native_relationships"]["status"] == "failed_no_mutation"


def test_native_mutation_failure_blocks_content_update(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a native relationship mutation failure must never reach content update."""
    payload = _minimal_input_with_relationships(
        repo_tmp, _base_relationships(add_blocked_by=[42])
    )

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    # Issue #1897 P0-1: candidate body guard/hygiene/readiness now run
    # *before* the native relationship mutation is attempted, so this test
    # (which exercises a native mutation failure) must let that content
    # validation pass in order to reach the relationship executor call.
    _stub_content_flow(monkeypatch, repo_tmp)

    calls: list[str] = []

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        calls.append(command_id)
        assert command_id == "issue_relationship.update"
        return _CP(1), {
            "status": "postcondition_rejected",
            "mutation_attempted": True,
            "before": {"parent": None, "blocked_by": [], "blocking": []},
            "desired": {"parent": None, "blocked_by": [42], "blocking": []},
            "after": {"parent": None, "blocked_by": [], "blocking": []},
            "completed_operations": [],
            "pending_operations": ["add_blocked_by:42"],
        }

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert calls == ["issue_relationship.update"]
    assert result["status"] == "failed_after_mutation"
    assert result["body_update"]["attempted"] is False
    assert result["native_relationships"]["status"] == "failed_after_mutation"
    assert result["native_relationships"]["attempted"] is True


@pytest.mark.parametrize(
    ("action", "target", "desired_parent"),
    [("set", 1860, 1860), ("remove", None, None)],
)
def test_native_parent_set_and_remove_readback(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    target: int | None,
    desired_parent: int | None,
) -> None:
    """AC3: parent set/remove executes and readback matches desired state."""
    expected_before_parent = None if action == "set" else 999
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(
            expected_before={"parent": expected_before_parent, "blocked_by": [], "blocking": []},
            parent={"action": action, "issue_number": target},
        ),
    )

    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            # Issue #1897 P1-7: after the native relationship mutation
            # succeeds, the transaction re-reads title/body/updatedAt before
            # attempting the content mutation.
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn,
        "_fetch_native_relationships",
        lambda *_a, **_k: ({"parent": expected_before_parent, "blocked_by": [], "blocking": []}, ""),
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    calls: list[str] = []

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        calls.append(command_id)
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": expected_before_parent, "blocked_by": [], "blocking": []},
                "desired": {"parent": desired_parent, "blocked_by": [], "blocking": []},
                "after": {"parent": desired_parent, "blocked_by": [], "blocking": []},
                "completed_operations": [f"{action}_parent:{target or expected_before_parent}"],
                "pending_operations": [],
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert calls[0] == "issue_relationship.update"
    assert result["status"] == "ok"
    assert result["native_relationships"]["after"]["parent"] == desired_parent


def _normal_input_with_new_body_from(payload: dict, repo_tmp: Path, new_body: str) -> dict:
    (repo_tmp / "tmp" / "new_body.md").write_text(new_body, encoding="utf-8")
    return payload


def test_native_blocked_by_add_remove_full_pagination_exact_set(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: add/remove blocked_by delegates to the executor's full-pagination
    exact-set readback; the edit transaction forwards the deduped/sorted
    add/remove sets and trusts the executor's after-state for the final
    order-independent comparison."""
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(
            expected_before={"parent": None, "blocked_by": [10, 20], "blocking": []},
            # Issue #1897 P1-1: add_blocked_by must already be sorted/unique
            # -- validate_issue_relationship_update_input() rejects
            # duplicate/unsorted raw input, and the caller no longer
            # deduplicates/sorts before validation.
            add_blocked_by=[5, 30],
            remove_blocked_by=[10],
        ),
    )
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn,
        "_fetch_native_relationships",
        lambda *_a, **_k: ({"parent": None, "blocked_by": [10, 20], "blocking": []}, ""),
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    written_payloads: list[dict] = []

    def _write_capture(issue_number: int, command_id: str, payload_obj: dict) -> str:
        written_payloads.append(payload_obj)
        return f"artifacts/{issue_number}/issue-metadata/{command_id}/x.input.json"

    monkeypatch.setattr(txn, "_write_issue_metadata_input", _write_capture)

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": None, "blocked_by": [10, 20], "blocking": []},
                "desired": {"parent": None, "blocked_by": [5, 20, 30], "blocking": []},
                # Full-pagination readback set is order-independent -- returned
                # here in non-sorted order to prove exact-set (not list-order)
                # equality is what the caller relies on.
                "after": {"parent": None, "blocked_by": [30, 20, 5], "blocking": []},
                "completed_operations": ["remove_blocked_by:10", "add_blocked_by:5", "add_blocked_by:30"],
                "pending_operations": [],
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] == "ok"
    sent = written_payloads[0]
    assert sent["add_blocked_by"] == [5, 30]
    assert sent["remove_blocked_by"] == [10]
    assert sorted(result["native_relationships"]["after"]["blocked_by"]) == [5, 20, 30]


def test_native_blocking_add_remove_full_pagination_exact_set(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: same exact-set contract for the `blocking` relation."""
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(
            expected_before={"parent": None, "blocked_by": [], "blocking": [7]},
            add_blocking=[9],
            remove_blocking=[7],
        ),
    )
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn,
        "_fetch_native_relationships",
        lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": [7]}, ""),
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": None, "blocked_by": [], "blocking": [7]},
                "desired": {"parent": None, "blocked_by": [], "blocking": [9]},
                "after": {"parent": None, "blocked_by": [], "blocking": [9]},
                "completed_operations": ["remove_blocking:7", "add_blocking:9"],
                "pending_operations": [],
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] == "ok"
    assert result["native_relationships"]["after"]["blocking"] == [9]


def test_final_readback_all_fields_match_required_for_ok(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: relationship success alone is not sufficient -- content final
    readback must also match, else overall status is not `ok`."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            # Issue #1897 P1-7: post-relationship-mutation refresh readback --
            # unchanged, so it does not itself trigger the concurrent-drift
            # guard.
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            # Final readback returns an unrelated body -- content mismatch.
            {"title": "old", "body": "DRIFTED body", "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": None, "blocked_by": [], "blocking": []},
                "desired": {"parent": None, "blocked_by": [3], "blocking": []},
                "after": {"parent": None, "blocked_by": [3], "blocking": []},
                "completed_operations": ["add_blocked_by:3"],
                "pending_operations": [],
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] == "failed_after_mutation"
    # Relationship half of the saga still succeeded and is reported as such.
    assert result["native_relationships"]["status"] == "applied"


def test_prose_mentions_do_not_set_native_relationships() -> None:
    """AC7: the module never derives parent/blocked_by/blocking from body
    prose (`Part of`, `Related`, URL mentions, comments). The only source of
    truth is the explicit, structured `native_relationships` transaction
    input field."""
    source = (SCRIPTS_DIR / "edit_issue_txn.py").read_text(encoding="utf-8")
    forbidden_patterns = [
        "Part of #",
        "Related Issue",
        're.search(r"depends',
        're.match(r"depends',
        "_collect_parent_candidates",
        "_extract_parent_issue_number_from_body",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in source, f"prose-derived relationship parsing found: {pattern!r}"
    # The only relationship-triggering input is native_relationships itself.
    assert 'input_data.get("native_relationships")' in source


def test_identical_desired_state_is_idempotent_no_op(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8: when current native state already equals desired state, the
    executor result carries status=no_op / mutation_attempted=False, and the
    transaction proceeds to content mutation without treating the relation
    step as a failure."""
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(expected_before={"parent": 1860, "blocked_by": [], "blocking": []},
                             parent={"action": "set", "issue_number": 1860}),
    )
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": 1860, "blocked_by": [], "blocking": []}, "")
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "no_op",
                "mutation_attempted": False,
                "before": {"parent": 1860, "blocked_by": [], "blocking": []},
                "desired": {"parent": 1860, "blocked_by": [], "blocking": []},
                "after": {"parent": 1860, "blocked_by": [], "blocking": []},
                "completed_operations": [],
                "pending_operations": [],
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] == "ok"
    assert result["native_relationships"]["status"] == "no_op"
    assert result["native_relationships"]["attempted"] is False


def test_partial_native_mutation_returns_failed_after_mutation(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC9: partial native mutation surfaces before/desired/after and
    completed/pending operations, and never reaches content mutation."""
    payload = _minimal_input_with_relationships(
        repo_tmp, _base_relationships(add_blocked_by=[1, 2])
    )

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    # Issue #1897 P0-1: candidate body guard/hygiene/readiness now run
    # before the native relationship mutation is attempted.
    _stub_content_flow(monkeypatch, repo_tmp)

    calls: list[str] = []

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        calls.append(command_id)
        return _CP(1), {
            "status": "partial",
            "mutation_attempted": True,
            "before": {"parent": None, "blocked_by": [], "blocking": []},
            "desired": {"parent": None, "blocked_by": [1, 2], "blocking": []},
            "after": {"parent": None, "blocked_by": [1], "blocking": []},
            "completed_operations": ["add_blocked_by:1"],
            "pending_operations": ["add_blocked_by:2"],
        }

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert calls == ["issue_relationship.update"]
    assert result["status"] == "failed_after_mutation"
    nr = result["native_relationships"]
    assert nr["before"] == {"parent": None, "blocked_by": [], "blocking": []}
    assert nr["desired"] == {"parent": None, "blocked_by": [1, 2], "blocking": []}
    assert nr["after"] == {"parent": None, "blocked_by": [1], "blocking": []}
    assert nr["completed_operations"] == ["add_blocked_by:1"]
    assert nr["pending_operations"] == ["add_blocked_by:2"]


def test_capability_preflight_missing_blocks_as_environment_not_write_permission_proof(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC10: a missing gh binary / unreachable auth is an environment
    blocker classified separately from a runtime write-permission
    rejection, and never falls back to body-only mutation."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (False, "gh_binary_not_found"))

    def _fail_native(*_a: object, **_k: object) -> tuple[dict | None, str]:
        pytest.fail("pre-readback must not run when capability preflight fails")

    monkeypatch.setattr(txn, "_fetch_native_relationships", _fail_native)

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("no executor invocation permitted; and success here would prove write access, not preflight")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    assert result["native_relationships"]["attempted"] is False
    codes = [e["code"] for e in result["native_relationships"]["errors"]]
    assert "relationship_capability_preflight_failed" in codes


def test_destructive_remove_blocked_by_expected_before_drift_fails_no_mutation(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC11: a destructive remove_blocked_by must never execute against a
    drifted pre-readback -- caller-declared expected_before is only ever
    trusted after comparing it to a fresh live readback."""
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(
            expected_before={"parent": None, "blocked_by": [5], "blocking": []},
            remove_blocked_by=[5],
        ),
    )

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    # Live state has drifted: blocked_by is now empty, not [5] as declared.
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("destructive remove must not mutate when expected_before has drifted")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    codes = [e["code"] for e in result["native_relationships"]["errors"]]
    assert "expected_before_drift_detected" in codes


@pytest.mark.parametrize(
    "relationships",
    [
        _base_relationships(parent={"action": "set", "issue_number": 1287}),  # self-parent
        _base_relationships(add_blocked_by=[1287]),  # self-dependency
        _base_relationships(add_blocked_by=[9], add_blocking=[9]),  # same-target conflict
    ],
)
def test_graph_invariant_violations_rejected_before_mutation(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, relationships: dict
) -> None:
    """AC12: self-parent, self-dependency, and blocked_by/blocking same-target
    conflicts are all rejected before any mutation is attempted."""
    payload = _minimal_input_with_relationships(repo_tmp, relationships)

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("graph invariant violation must block mutation entirely")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    codes = [e["code"] for e in result["native_relationships"]["errors"]]
    assert "graph_invariant_violation" in codes


def test_body_parent_rebind_updates_native_parent_1679_fixture(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC16: a #1679-style body parent rebind (`parent_issue` changed from a
    closed old parent to a new parent) must also update the native parent,
    and the transaction only reports `ok` once the native readback confirms
    the new parent."""
    rebind_body = (
        "## Machine-Readable Contract\n\n```yaml\nparent_issue: \"#1860\"\n```\n\n## Parent Issue\n\n#1860\n"
    )
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(
            expected_before={"parent": 1674, "blocked_by": [], "blocking": []},
            parent={"action": "set", "issue_number": 1860},
        ),
    )
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": rebind_body, "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": 1674, "blocked_by": [], "blocking": []}, "")
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": 1674, "blocked_by": [], "blocking": []},
                "desired": {"parent": 1860, "blocked_by": [], "blocking": []},
                "after": {"parent": 1860, "blocked_by": [], "blocking": []},
                "completed_operations": ["remove_parent:1674", "set_parent:1860"],
                "pending_operations": [],
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text(rebind_body)}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, rebind_body))
    assert result["status"] == "ok"
    assert result["native_relationships"]["after"]["parent"] == 1860


# ---------------------------------------------------------------------------
# PR #1897 iteration-4 fix_delta: transaction-integrity regression tests
# ---------------------------------------------------------------------------


def test_missing_body_file_never_invokes_relationship_executor(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-1 bullet 1: new_body_file is read (and must exist) before any
    native relationship mutation is attempted."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))
    payload["new_body_file"] = "tmp/does_not_exist.md"

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("relationship executor must never run before new_body_file is read")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    with pytest.raises(ValueError):
        txn.run_transaction(payload)


def test_body_guard_failure_never_invokes_relationship_executor(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-1: candidate body guard failure blocks the native relationship
    mutation entirely, and the transaction reports no native mutation
    attempt at all."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(2, stderr="guard failed")
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("relationship executor must never run when candidate body guard fails")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["native_relationships"]["attempted"] is False
    assert result["native_relationships"]["status"] == "not_run"


def test_readiness_failure_never_invokes_relationship_executor(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-1: candidate body readiness failure blocks the native relationship
    mutation entirely."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(1, stderr="readiness failed")
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("relationship executor must never run when candidate body readiness fails")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["native_relationships"]["attempted"] is False


def test_exception_after_native_apply_never_reports_failed_no_mutation(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-1 bullet 5: an unexpected exception raised after a native
    relationship mutation has already been attempted must never surface as
    failed_no_mutation."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": None, "blocked_by": [], "blocking": []},
                "desired": {"parent": None, "blocked_by": [3], "blocking": []},
                "after": {"parent": None, "blocked_by": [3], "blocking": []},
                "completed_operations": ["add_blocked_by:3"],
                "pending_operations": [],
            }
        raise RuntimeError("simulated unexpected failure after native apply")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] != "failed_no_mutation"
    assert result["mutation_started"] is True
    assert result["native_relationships"]["status"] == "applied"


def test_native_apply_then_body_failure_reports_failed_after_mutation(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-1: a real native relationship mutation followed by a body
    controlled-executor failure must report failed_after_mutation, not
    failed_no_mutation."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": None, "blocked_by": [], "blocking": []},
                "desired": {"parent": None, "blocked_by": [3], "blocking": []},
                "after": {"parent": None, "blocked_by": [3], "blocking": []},
                "completed_operations": ["add_blocked_by:3"],
                "pending_operations": [],
            }
        return _CP(1), {"status": "failed"}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] != "failed_no_mutation"
    assert result["mutation_started"] is True
    assert result["native_relationships"]["status"] == "applied"


def test_final_combined_readback_rejects_parent_drift(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-2: a native parent drift observed after content mutation completes
    must prevent an `ok` result."""
    payload = _minimal_input_with_relationships(
        repo_tmp,
        _base_relationships(
            expected_before={"parent": None, "blocked_by": [], "blocking": []},
            parent={"action": "set", "issue_number": 1860},
        ),
    )
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    calls: list[str] = []

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        calls.append(command_id)
        if command_id == "issue_relationship.update":
            if len([c for c in calls if c == "issue_relationship.update"]) == 1:
                return _CP(0), {
                    "status": "applied",
                    "mutation_attempted": True,
                    "before": {"parent": None, "blocked_by": [], "blocking": []},
                    "desired": {"parent": 1860, "blocked_by": [], "blocking": []},
                    "after": {"parent": 1860, "blocked_by": [], "blocking": []},
                    "completed_operations": ["set_parent:1860"],
                    "pending_operations": [],
                }
            # Final combined readback (Phase C) -- someone else changed the
            # parent after Phase B confirmed it.
            return _CP(0), {
                "status": "precondition_rejected",
                "mutation_attempted": False,
                "before": {"parent": 999, "blocked_by": [], "blocking": []},
                "desired": {"parent": 1860, "blocked_by": [], "blocking": []},
                "after": None,
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] == "failed_after_mutation"
    codes = [e["code"] for e in result["native_relationships"]["errors"]]
    assert "final_native_relationship_readback_drift" in codes


def test_final_combined_readback_rejects_blocked_by_drift(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-2: a native blocked_by drift observed after content mutation
    completes must prevent an `ok` result, mirroring the parent-drift
    regression test above for the blocked_by relation."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
            {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    _stub_content_flow(monkeypatch, repo_tmp)

    calls: list[str] = []

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        calls.append(command_id)
        if command_id == "issue_relationship.update":
            if len([c for c in calls if c == "issue_relationship.update"]) == 1:
                return _CP(0), {
                    "status": "applied",
                    "mutation_attempted": True,
                    "before": {"parent": None, "blocked_by": [], "blocking": []},
                    "desired": {"parent": None, "blocked_by": [3], "blocking": []},
                    "after": {"parent": None, "blocked_by": [3], "blocking": []},
                    "completed_operations": ["add_blocked_by:3"],
                    "pending_operations": [],
                }
            # Final combined readback: blocked_by has drifted since Phase B.
            return _CP(0), {
                "status": "precondition_rejected",
                "mutation_attempted": False,
                "before": {"parent": None, "blocked_by": [], "blocking": []},
                "desired": {"parent": None, "blocked_by": [3], "blocking": []},
                "after": None,
            }
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_normal_input_with_new_body_from(payload, repo_tmp, "new issue body"))
    assert result["status"] == "failed_after_mutation"
    codes = [e["code"] for e in result["native_relationships"]["errors"]]
    assert "final_native_relationship_readback_drift" in codes


def test_raw_duplicate_relationship_input_is_rejected(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1: duplicate entries in add_blocked_by must be rejected by the
    graph-invariant validator, not silently deduplicated before
    validation."""
    payload = _minimal_input_with_relationships(
        repo_tmp, _base_relationships(add_blocked_by=[5, 5, 30])
    )

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("malformed duplicate relationship input must be rejected before any mutation")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    codes = [e["code"] for e in result["native_relationships"]["errors"]]
    assert "graph_invariant_violation" in codes


def test_raw_unsorted_relationship_input_is_rejected(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1: unsorted entries in add_blocked_by must be rejected, not
    silently sorted before validation."""
    payload = _minimal_input_with_relationships(
        repo_tmp, _base_relationships(add_blocked_by=[30, 5])
    )

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        pytest.fail("malformed unsorted relationship input must be rejected before any mutation")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    codes = [e["code"] for e in result["native_relationships"]["errors"]]
    assert "graph_invariant_violation" in codes


def test_child_timeout_triggers_independent_readback(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-5: a controlled-executor child timeout after launch must trigger
    an independent readback rather than being assumed to be
    failed_no_mutation."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))

    def _fetch(*_a: object, **_k: object) -> tuple[dict, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    _stub_content_flow(monkeypatch, repo_tmp)

    readback_calls = {"count": 0}

    def _fetch_native(*_a: object, **_k: object) -> tuple[dict | None, str]:
        readback_calls["count"] += 1
        if readback_calls["count"] == 1:
            return {"parent": None, "blocked_by": [], "blocking": []}, ""
        # Independent post-timeout readback: the mutation actually landed.
        return {"parent": None, "blocked_by": [3], "blocking": []}, ""

    monkeypatch.setattr(txn, "_fetch_native_relationships", _fetch_native)

    def _invoke(*_a: object, **_k: object) -> tuple[_CP, dict | None]:
        return _CP(124, stderr="child command timeout after 30s"), None

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert readback_calls["count"] == 2
    nr = result["native_relationships"]
    assert nr["attempted"] is True
    assert nr["status"] != "failed_no_mutation"
    assert result["status"] != "failed_no_mutation"


def test_native_only_apply_is_not_reported_as_no_change(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-5: when native_relationships applies a real change but title/body
    are unchanged, the top-level transaction must not report `no_change` --
    a real remote mutation occurred."""
    payload = _minimal_input_with_relationships(repo_tmp, _base_relationships(add_blocked_by=[3]))
    payload["comment_mode"] = {"mode": "skip"}
    readbacks = iter(
        [
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
            {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:41:00Z"},
        ]
    )
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_a, **_k: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_relationship_capability_preflight", lambda: (True, ""))
    monkeypatch.setattr(
        txn, "_fetch_native_relationships", lambda *_a, **_k: ({"parent": None, "blocked_by": [], "blocking": []}, "")
    )
    # `_minimal_input` always sets new_body_file content to "new issue body";
    # override it here so the body itself is unchanged (only the native
    # relationship changes).
    (repo_tmp / "tmp" / "new_body.md").write_text("old issue body", encoding="utf-8")

    def _invoke(command_id: str, *_a: object, **_k: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_relationship.update":
            return _CP(0), {
                "status": "applied",
                "mutation_attempted": True,
                "before": {"parent": None, "blocked_by": [], "blocking": []},
                "desired": {"parent": None, "blocked_by": [3], "blocking": []},
                "after": {"parent": None, "blocked_by": [3], "blocking": []},
                "completed_operations": ["add_blocked_by:3"],
                "pending_operations": [],
            }
        pytest.fail("content executor must not be invoked when title/body are unchanged")

    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] != "no_change"
    assert result["status"] == "ok"
    assert result["mutation_started"] is True
    assert result["native_relationships"]["status"] == "applied"


def test_total_count_mismatch_rejects_exact_set(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-4: _fetch_native_relationships fails closed (rather than
    reporting a truncated exact set) when GraphQL totalCount does not match
    the number of nodes actually returned on a single page."""
    import json as _json

    def _run(_args: list[str], **_kwargs: object) -> _CP:
        payload = {
            "data": {
                "repository": {
                    "issue": {
                        "parent": None,
                        "blockedBy": {
                            "pageInfo": {"hasNextPage": False},
                            "totalCount": 5,
                            "nodes": [{"number": 1}],
                        },
                        "blocking": {"pageInfo": {"hasNextPage": False}, "totalCount": 0, "nodes": []},
                    }
                }
            }
        }
        return _CP(0, stdout=_json.dumps(payload))

    monkeypatch.setattr(txn, "_run_command", _run)
    result, err = txn._fetch_native_relationships(1287, "squne121/agent-platform")
    assert result is None
    assert err == "blocked_by_total_count_mismatch"
