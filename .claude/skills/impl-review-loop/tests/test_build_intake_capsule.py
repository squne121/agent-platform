"""Tests for build_intake_capsule.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import builtins
import sys
import types
from pathlib import Path
from unittest.mock import patch


TEST_REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    TEST_REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "build_intake_capsule.py"
)
PREPARATION_MD = (
    TEST_REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "preparation.md"
)

spec = importlib.util.spec_from_file_location("build_intake_capsule", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _issue_view_json(
    *,
    title: str = "実装: intake capsule テスト",
    body: str = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n",
    updated_at: str = "2026-06-19T00:00:00Z",
    labels: list[dict[str, str]] | None = None,
) -> str:
    if labels is None:
        labels = [{"name": "phase/implementation"}]
    return json.dumps(
        {
            "title": title,
            "state": "open",
            "labels": labels,
            "body": body,
            "updatedAt": updated_at,
        }
    )


# #1475: this test module exercises routing / triage / normalization logic,
# not the trust policy itself (that is covered end-to-end in
# test_contract_snapshot_author_binding.py). Default every fixture comment
# to the sole allowlisted TRUSTED_CONTRACT_PUBLISHERS identity so existing
# routing assertions are unaffected by the fix_delta P1 item 1/2 trust gate;
# tests that specifically need an untrusted comment pass author=... overrides.
_TRUSTED_AUTHOR_LOGIN = "squne121"
_TRUSTED_AUTHOR_ID = 63350259
_TRUSTED_AUTHOR_TYPE = "User"
_TRUSTED_AUTHOR_ASSOCIATION = "OWNER"


def _comment_ndjson(
    body: str,
    *,
    comment_id: int = 1,
    created_at: str = "2026-06-19T00:01:00Z",
    author: str | None = _TRUSTED_AUTHOR_LOGIN,
    author_id: int | None = _TRUSTED_AUTHOR_ID,
    author_type: str | None = _TRUSTED_AUTHOR_TYPE,
    author_association: str | None = _TRUSTED_AUTHOR_ASSOCIATION,
) -> str:
    return json.dumps(
        {
            "id": comment_id,
            "html_url": f"https://github.com/squne121/agent-platform/issues/958#issuecomment-{comment_id}",
            "created_at": created_at,
            "updated_at": created_at,
            "body": body,
            "author": author,
            "author_id": author_id,
            "author_type": author_type,
            "author_association": author_association,
        }
    )


def _run_command_side_effect_factory(commands):
    calls = {"i": 0}

    def _run(cmd):
        index = calls["i"]
        calls["i"] += 1
        return commands[index]

    return _run


def test_ac1_contract_snapshot_missing_routes_to_proceed_to_step_1():
    """#1851: missing_go is advisory -- it must not hard-block Step 1."""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"
    assert capsule["next_action"]["route"] == "proceed_to_step_1"
    assert artifact["source_integrity"]["parse_warnings"]["invalid_json_lines_count"] == 0


def test_ac2_blocked_categories_from_ensure_contract_snapshot_result(tmp_path):
    ensure_payload = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "status": "blocked_needs_refinement",
        "contract_review_once_result": {
            "vc_preflight_classifications": [
                {
                    "ac": "AC1",
                    "category": "package_manager_no_tty_prompt",
                    "decision": "blocked",
                    "raw_command": "pnpm build",
                },
                {
                    "ac": "AC2",
                    "category": "vc_no_tests_collected",
                    "decision": "blocked",
                    "raw_command": "uv run pytest -k foo",
                    "exit_code": 5,
                    "subreason": "pytest_k_filter_matches_no_tests",
                },
            ]
        },
    }
    ensure_path = tmp_path / "ensure-result.json"
    ensure_path.write_text(json.dumps(ensure_payload), encoding="utf-8")

    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            ensure_contract_snapshot_result=str(ensure_path),
        )

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"
    assert capsule["contract_snapshot"]["top_blocked_categories"] == [
        "package_manager_no_tty_prompt",
        "vc_no_tests_collected",
    ]
    assert capsule["contract_snapshot"]["contract_blocker_triage"]["schema"] == "CONTRACT_BLOCKER_TRIAGE_V1"
    assert capsule["next_action"]["route"] == "proceed_to_step_1"


def test_ac3_main_dirty_summary_shows_count_and_sample_only():
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "M  a.txt\n?? b.txt\nD  c.txt\nA  d.txt\n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    summary = capsule["repo_state"]["dirty_paths_summary"]
    assert summary["count"] == 4
    assert summary["sample_paths"] == ["a.txt", "b.txt", "c.txt", "d.txt"]
    assert summary["truncated"] is False


def test_ac4_stdout_budget_and_artifact_path(tmp_path):
    huge_line = "x" * 8000
    capsule = {
        "schema": mod._SCHEMA_NAME,
        "schema_version": mod._SCHEMA_VERSION,
        "issue_number": 958,
        "repo": "squne121/agent-platform",
        "issue_ready_tuple": {"status": "pass"},
        "contract_snapshot": {"normalized_status": "missing_go", "top_blocked_categories": []},
        "source_integrity": {"parse_warnings": {}, "evidence_complete": True},
        "worktree": {},
        "repo_state": {"dirty": False, "dirty_paths_summary": {"count": 0, "sample_paths": []}},
        "agent_runtime": {},
        "next_action": {"route": "ensure_contract_snapshot"},
        "warnings": [],
        "errors": [],
        "tail": huge_line,
    }
    artifact = {"schema": mod._SCHEMA_NAME, "schema_version": mod._SCHEMA_VERSION, "detail": huge_line}

    with patch.object(mod, "build_intake_capsule", return_value=(capsule, artifact, 0)):
        with patch.object(
            mod.sys,
            "argv",
            [
                "build_intake_capsule.py",
                "--issue-number",
                "958",
                "--artifact-dir",
                str(tmp_path / "artifacts"),
                "--max-stdout-bytes",
                "4096",
            ],
        ):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                exit_code = mod.main()

    assert exit_code == 0
    output = captured.getvalue()
    assert len(output.encode("utf-8")) <= 4096
    artifact_path = tmp_path / "artifacts" / "intake-capsule-958.json"
    assert artifact_path.exists()
    loaded_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert loaded_artifact["artifact_settings"]["max_stdout_bytes"] == 4096


def test_ac5_stdout_does_not_include_full_skill_body():
    yaml_body = """
<!-- loop-protocol:contract-snapshot -->

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/agent-platform/issues/958
  body_sha256: "sha256:deadbeef"
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(yaml_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, _ = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    printed = mod._render_stdout(capsule, 4096)
    assert "## Machine-Readable Contract" not in printed
    assert "status: full-body" not in printed
    assert "<!-- loop-protocol:contract-snapshot -->" not in printed


def test_latest_blocked_routes_to_triage():
    blocked_body = """
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/agent-platform/issues/958
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(blocked_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "latest_blocked"
    assert capsule["next_action"]["route"] == "run_contract_blocker_triage"


def test_malformed_ndjson_sets_parse_warning():
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, '{"id":1,"body":"ok"}\nnot-json\n', ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert "invalid_json_lines_count:1" in capsule["warnings"]
    assert artifact["source_integrity"]["parse_warnings"]["invalid_json_lines_count"] == 1


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint routing (Issue #1537 AC3)
# ---------------------------------------------------------------------------


def _fingerprint_yaml_block(
    comment_id: int = 1, body_sha256: str | None = None, paths_hash: str | None = None
) -> str:
    fingerprint = {
        "issue_number": 958,
        "contract_source_kind": "issue_comment",
        "contract_source_id": str(comment_id),
        "contract_body_sha256": body_sha256 or "sha256:" + "a" * 64,
        "allowed_paths_normalized_sha256": paths_hash or "b" * 64,
        "base_ref": "main",
        "base_sha_at_snapshot": "c" * 40,
    }
    return "\n  expected_contract_fingerprint: '" + json.dumps(
        fingerprint, separators=(",", ":")
    ).replace("'", "''") + "'\n"


def test_ac3_go_with_fingerprint_routes_to_proceed():
    comment_id = 42
    issue_body = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n"
    default_body_sha256 = mod._sha256(issue_body)
    paths_hash = mod._live_allowed_paths_hash(issue_body)
    go_body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/agent-platform/issues/958
  body_sha256: "{default_body_sha256}"{_fingerprint_yaml_block(comment_id, default_body_sha256, paths_hash)}```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(go_body, comment_id=comment_id), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "go"
    assert capsule["next_action"]["route"] == "proceed_to_step_1"


def test_ac3_canonical_fingerprint_reaches_consumer_via_forced_fallback(monkeypatch):
    """GIVEN canonical producer output WHEN PyYAML is unavailable THEN intake accepts it."""
    comment_id = 42
    issue_body = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n"
    body_sha256 = mod._sha256(issue_body)
    paths_hash = mod._live_allowed_paths_hash(issue_body)
    go_body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/agent-platform/issues/958
  body_sha256: "{body_sha256}"{_fingerprint_yaml_block(comment_id, body_sha256, paths_hash)}```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(go_body, comment_id=comment_id), ""),
        ]
    )
    original_import = builtins.__import__

    def reject_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("forced fallback")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_yaml)
    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "go"
    assert artifact["source_integrity"]["parse_warnings"]["invalid_contract_blocks_count"] == 0


def test_ac4_noncanonical_flow_fingerprint_is_rejected_by_intake_with_parser_parity(monkeypatch):
    """GIVEN a flow mapping WHEN either parser runs THEN intake keeps it non-authoritative."""
    issue_body = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n"
    body_sha256 = mod._sha256(issue_body)
    fingerprint = {
        "issue_number": 958,
        "contract_source_kind": "issue_comment",
        "contract_source_id": "42",
        "contract_body_sha256": body_sha256,
        "allowed_paths_normalized_sha256": mod._live_allowed_paths_hash(issue_body),
        "base_ref": "main",
        "base_sha_at_snapshot": "c" * 40,
    }
    go_body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/agent-platform/issues/958
  body_sha256: "{body_sha256}"
  expected_contract_fingerprint: {json.dumps(fingerprint, separators=(",", ":"))}
```
"""

    original_import = builtins.__import__

    for force_fallback in (False, True):
        run_cmd = _run_command_side_effect_factory(
            [
                (0, _issue_view_json(), ""),
                (0, "abc\n", ""),
                (0, "main\n", ""),
                (0, "", ""),
                (0, _comment_ndjson(go_body, comment_id=42), ""),
            ]
        )

        def maybe_reject_yaml(name, *args, **kwargs):
            if force_fallback and name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        with monkeypatch.context() as nested_patch:
            nested_patch.setattr(builtins, "__import__", maybe_reject_yaml)
            with patch.object(mod, "_run_command", side_effect=run_cmd):
                capsule, artifact, exit_code = mod.build_intake_capsule(
                    958, "squne121/agent-platform", None
                )

        assert exit_code == 0
        assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"
        assert artifact["source_integrity"]["parse_warnings"]["invalid_contract_blocks_count"] == 1


def test_ac3_go_without_fingerprint_routes_to_missing_go_and_proceeds_advisory():
    """A trusted, schema-valid `status: go` that lacks a well-formed
    source-bound expected_contract_fingerprint must never be treated as a
    loop-consumable fresh go -- normalized_status stays missing_go so
    ensure_contract_snapshot re-materialization is still recorded as the
    correct remediation, but (#1851) routing itself is advisory and still
    proceeds to Step 1."""
    go_body = """
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/agent-platform/issues/958
  body_sha256: "sha256:deadbeef"
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(go_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"
    assert capsule["next_action"]["route"] == "proceed_to_step_1"


def test_ac3_go_with_fingerprint_wrong_issue_number_routes_to_missing_go():
    """A fingerprint whose issue_number does not match the issue this parse
    run is scoped to must never be accepted as fingerprint-ready."""
    go_body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/agent-platform/issues/958
  body_sha256: "sha256:deadbeef"
  expected_contract_fingerprint:
    issue_number: 1
    contract_source_kind: issue_comment
    contract_source_id: "1"
    contract_body_sha256: "sha256:{'a' * 64}"
    allowed_paths_normalized_sha256: "{'b' * 64}"
    base_ref: main
    base_sha_at_snapshot: "{'c' * 40}"
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(go_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"


def test_ac6_preparation_refers_to_capsule_before_redundant_commands():
    body = PREPARATION_MD.read_text(encoding="utf-8")
    idx_capsule_section = body.find("## 0-a. Intake capsule-first")
    idx_step0_section = body.find("## 0. Intake Gate")
    idx_capsule_doc = body.find("build_intake_capsule.py")

    assert idx_capsule_section >= 0
    assert idx_step0_section > idx_capsule_section
    assert idx_capsule_doc > idx_capsule_section
    assert "同一 loop 内では `gh issue view` / comments fetch / main `git status` / snapshot 探索を再実行しない" in body
    assert "run_contract_blocker_triage" in body


# ---------------------------------------------------------------------------
# #1869 fix_delta P0-4: fatal_errors / warnings split
# ---------------------------------------------------------------------------


def test_comments_fetch_failure_is_advisory_warning_not_fatal_exit():
    """Comment fetch failure (gh api non-zero) must land in `warnings`, not
    `fatal_errors`, and must not force a non-zero exit code."""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (1, "", "gh: rate limited"),  # issue_comments fetch fails
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert "comments_fetch_error" in capsule["warnings"]
    assert "comments_fetch_error" not in capsule["fatal_errors"]
    assert capsule["fatal_errors"] == []
    assert artifact["warnings"] == capsule["warnings"]


def test_live_issue_not_accessible_is_fatal_exit_1():
    """gh issue view failure (live Issue not accessible / target identity
    unresolvable) remains a genuine fatal_errors condition -> exit 1."""
    run_cmd = _run_command_side_effect_factory(
        [
            (1, "", "gh: issue not found"),  # issue_view fails
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 1
    assert "issue_view_failed" in capsule["fatal_errors"]


def test_ensure_contract_snapshot_parse_error_is_advisory_warning(tmp_path):
    """A malformed --ensure-contract-snapshot-result file is a warning
    (snapshot fetch/parse failure), not fatal."""
    bad_file = tmp_path / "bad_snapshot.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958, "squne121/agent-platform", str(bad_file)
        )

    assert exit_code == 0
    assert "ensure_contract_snapshot_result_parse_error" in capsule["warnings"]
    assert capsule["fatal_errors"] == []


# ---------------------------------------------------------------------------
# Issue #2606: comment-ID direct lookup for PR-side conversation comments
# (map miss in the target Issue's own comments_by_id).
# ---------------------------------------------------------------------------

_PR_NUMBER = 4242
_PR_COMMENT_ID = 6000000001


def _pr_comment_lookup_json(
    *,
    comment_id: int,
    html_url: str,
    body: str = "PR conversation comment body.",
    author: str = _TRUSTED_AUTHOR_LOGIN,
    author_id: int = _TRUSTED_AUTHOR_ID,
    author_type: str = _TRUSTED_AUTHOR_TYPE,
    author_association: str = _TRUSTED_AUTHOR_ASSOCIATION,
    updated_at: str = "2026-06-19T00:02:00Z",
) -> str:
    return json.dumps(
        {
            "id": comment_id,
            "html_url": html_url,
            "created_at": updated_at,
            "updated_at": updated_at,
            "body": body,
            "author": author,
            "author_id": author_id,
            "author_type": author_type,
            "author_association": author_association,
        }
    )


def _closing_issues_json(issue_numbers: list[int], repo: str = "squne121/agent-platform") -> str:
    owner, name = repo.split("/", 1)
    return json.dumps(
        {
            "closingIssuesReferences": [
                {
                    "number": n,
                    "url": f"https://github.com/{repo}/issues/{n}",
                    "repository": {"name": name, "owner": {"login": owner}},
                }
                for n in issue_numbers
            ]
        }
    )


def test_ac2_pr_comment_direct_lookup_resolves_human_context_url():
    """#2606 AC2: a PR conversation comment absent from the target Issue's
    own comments_by_id map resolves via the read-only comment-ID direct
    lookup when the PR's `closingIssuesReferences` names the target Issue."""
    pr_comment_url = f"https://github.com/squne121/agent-platform/pull/{_PR_NUMBER}#issuecomment-{_PR_COMMENT_ID}"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),  # target Issue's own comments -- PR comment is not among them
            (0, _pr_comment_lookup_json(comment_id=_PR_COMMENT_ID, html_url=pr_comment_url), ""),
            (0, _closing_issues_json([958]), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            human_context_comment_urls=[pr_comment_url],
        )

    assert exit_code == 0, capsule
    human_supplied = capsule["context_inputs"]["human_supplied"]
    assert len(human_supplied) == 1
    assert human_supplied[0]["comment_id"] == _PR_COMMENT_ID
    assert human_supplied[0]["url"] == pr_comment_url
    assert artifact["context_inputs"]["human_supplied"][0]["body"] == "PR conversation comment body."


def test_ac3_pr_comment_direct_lookup_resolves_agent_report_url_with_schema_validation():
    """#2606 AC3: the same PR-side direct-lookup path applies to
    --agent-report-comment-url, and the existing structured-schema
    validation (allowlisted top-level fenced block) still runs."""
    agent_comment_id = 6000000002
    pr_comment_url = f"https://github.com/squne121/agent-platform/pull/{_PR_NUMBER}#issuecomment-{agent_comment_id}"
    agent_body = "```yaml\nIMPLEMENT_RESULT_V1:\n  status: ok\n```\n"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
            (
                0,
                _pr_comment_lookup_json(
                    comment_id=agent_comment_id,
                    html_url=pr_comment_url,
                    body=agent_body,
                    author="github-actions",
                    author_id=41898282,
                    author_type="Bot",
                    author_association="NONE",
                ),
                "",
            ),
            (0, _closing_issues_json([958]), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            agent_report_comment_urls=[pr_comment_url],
        )

    assert exit_code == 0, capsule
    agent_generated = capsule["context_inputs"]["agent_generated"]
    assert len(agent_generated) == 1
    assert agent_generated[0]["comment_id"] == agent_comment_id
    assert agent_generated[0]["validated_schema_id"] == "IMPLEMENT_RESULT_V1"
    assert agent_generated[0]["validation_status"] == "ok"


def test_ac4_pr_comment_not_closing_target_issue_is_fail_closed():
    """#2606 AC4: a PR conversation comment on a PR that does NOT list the
    target Issue in `closingIssuesReferences` must fail-closed -- an
    unrelated PR comment must never be accepted."""
    pr_comment_url = f"https://github.com/squne121/agent-platform/pull/{_PR_NUMBER}#issuecomment-{_PR_COMMENT_ID}"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
            (0, _pr_comment_lookup_json(comment_id=_PR_COMMENT_ID, html_url=pr_comment_url), ""),
            (0, _closing_issues_json([1111]), ""),  # closes a DIFFERENT issue
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            human_context_comment_urls=[pr_comment_url],
        )

    assert exit_code == 1, capsule
    assert any(
        err.startswith("human_supplied_comment_pr_not_closing_target_issue:")
        for err in capsule["fatal_errors"]
    ), capsule


def test_pr_closes_same_numbered_issue_in_different_repo_fails_closed():
    """#2606 fix_delta (PR #2614 review comment): a PR whose
    `closingIssuesReferences` names an issue with the SAME number as the
    target Issue but in a DIFFERENT repository must never be accepted as
    closing the target Issue -- `number` alone is not sufficient identity,
    `url` (repo-qualified) must also match."""
    pr_comment_url = f"https://github.com/squne121/agent-platform/pull/{_PR_NUMBER}#issuecomment-{_PR_COMMENT_ID}"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
            (0, _pr_comment_lookup_json(comment_id=_PR_COMMENT_ID, html_url=pr_comment_url), ""),
            (0, _closing_issues_json([958], repo="other-owner/other-repo"), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            human_context_comment_urls=[pr_comment_url],
        )

    assert exit_code == 1, capsule
    assert any(
        err.startswith("human_supplied_comment_pr_not_closing_target_issue:")
        for err in capsule["fatal_errors"]
    ), capsule


def test_ac6_direct_lookup_html_url_readback_mismatch_is_fail_closed():
    """#2606 AC6: if the direct-lookup readback `html_url` does not exactly
    match the input URL, resolution fails closed (defends against a
    comment-ID collision / spoofed URL)."""
    requested_url = f"https://github.com/squne121/agent-platform/pull/{_PR_NUMBER}#issuecomment-{_PR_COMMENT_ID}"
    different_html_url = f"https://github.com/squne121/agent-platform/pull/9999#issuecomment-{_PR_COMMENT_ID}"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
            (0, _pr_comment_lookup_json(comment_id=_PR_COMMENT_ID, html_url=different_html_url), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            human_context_comment_urls=[requested_url],
        )

    assert exit_code == 1, capsule
    assert any(
        err.startswith("human_supplied_comment_url_html_url_mismatch:")
        for err in capsule["fatal_errors"]
    ), capsule


def test_ac6_issue_shaped_direct_lookup_wrong_issue_number_is_fail_closed():
    """#2606 AC6: an `/issues/N#issuecomment-ID` URL resolved via direct
    lookup must have N == target Issue number; a foreign Issue's comment
    (even with a matching html_url readback) fails closed."""
    foreign_comment_id = 6000000003
    foreign_issue_url = (
        f"https://github.com/squne121/agent-platform/issues/1234#issuecomment-{foreign_comment_id}"
    )
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
            (0, _pr_comment_lookup_json(comment_id=foreign_comment_id, html_url=foreign_issue_url), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            human_context_comment_urls=[foreign_issue_url],
        )

    assert exit_code == 1, capsule
    assert any(
        err.startswith("human_supplied_comment_issue_number_mismatch:")
        for err in capsule["fatal_errors"]
    ), capsule


def test_ac7_pr_inline_review_comment_url_shape_is_not_accepted():
    """#2606 AC7: a PR inline review comment (`#discussion_r<ID>` permalink
    shape) is out of scope and must not be routed through direct lookup at
    all -- it fails the initial `#issuecomment-` suffix parse, exactly like
    an unparseable URL, and never triggers a `gh api` call."""
    inline_review_url = f"https://github.com/squne121/agent-platform/pull/{_PR_NUMBER}#discussion_r123456789"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            human_context_comment_urls=[inline_review_url],
        )

    assert exit_code == 1, capsule
    assert any(
        err.startswith("human_supplied_comment_url_unparseable:")
        for err in capsule["fatal_errors"]
    ), capsule


def test_cross_repo_direct_lookup_url_is_fail_closed_without_extra_gh_call():
    """A comment URL shaped for a different repo must fail closed on the
    repo-binding check alone, without issuing an extra `gh api` call (no
    6th mocked command is provided -- an IndexError would surface if the
    implementation attempted a lookup)."""
    foreign_repo_url = f"https://github.com/someone-else/other-repo/issues/958#issuecomment-{_PR_COMMENT_ID}"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/agent-platform",
            None,
            human_context_comment_urls=[foreign_repo_url],
        )

    assert exit_code == 1, capsule
    assert any(
        err.startswith("human_supplied_comment_repo_mismatch:") for err in capsule["fatal_errors"]
    ), capsule


# ---------------------------------------------------------------------------
# #2713 AC10 (PR #2741 review, P2): `_load_module()`'s register-before-exec
# must not destroy a same-named `sys.modules` entry that already existed
# before the loader ran -- only the entry the loader itself inserted is ever
# removed on failure; a pre-existing entry is restored.
# ---------------------------------------------------------------------------


def _patch_spec_from_file_location_to_fail(monkeypatch, expected_name):
    """Return a REAL `ModuleSpec` (built via the genuine
    `spec_from_file_location()`) so `module_from_spec()` / the import
    machinery see a fully well-formed spec, with only `loader.exec_module()`
    swapped out to raise -- isolates the `exec_module()` failure path
    without hand-rolling a fake spec/loader pair that the frozen import
    machinery rejects for missing attributes (`origin`,
    `submodule_search_locations`, etc.)."""
    original_spec_from_file_location = importlib.util.spec_from_file_location

    def fake_spec_from_file_location(name, path):
        assert name == expected_name
        spec = original_spec_from_file_location(name, path)

        def failing_exec_module(module):  # noqa: ARG001 - matches Loader.exec_module signature
            raise RuntimeError("simulated exec_module failure")

        spec.loader.exec_module = failing_exec_module
        return spec

    monkeypatch.setattr(importlib.util, "spec_from_file_location", fake_spec_from_file_location)


def test_load_module_success_leaves_module_registered_under_its_own_name():
    """#2713 AC10: the success path is unaffected by the restore-on-failure
    logic -- the freshly executed module stays registered under its own
    `sys.modules` name."""
    module_name = "build_intake_capsule_load_module_success_test"
    sys.modules.pop(module_name, None)
    try:
        loaded = mod._load_module(SCRIPT_PATH, module_name)
        assert loaded is not None
        assert sys.modules.get(module_name) is loaded
    finally:
        sys.modules.pop(module_name, None)


def test_load_module_removes_only_its_own_newly_inserted_entry_on_failure(monkeypatch):
    """#2713 AC10 (PR #2741 review, P2): WHEN `sys.modules` has NO prior
    entry under the loader's module name AND `exec_module()` fails THEN
    `_load_module()` pop()s the entry it itself inserted -- no dangling
    half-initialized module is left behind."""
    module_name = "build_intake_capsule_load_module_no_prior_test"
    sys.modules.pop(module_name, None)

    _patch_spec_from_file_location_to_fail(monkeypatch, module_name)

    try:
        mod._load_module(SCRIPT_PATH, module_name)
        raise AssertionError("expected RuntimeError to propagate")
    except RuntimeError:
        pass
    assert module_name not in sys.modules


def test_load_module_restores_prior_entry_on_failure(monkeypatch):
    """#2713 AC10 (PR #2741 review, P2): WHEN `sys.modules` ALREADY has an
    entry under the loader's module name (from an unrelated prior load)
    AND `exec_module()` fails THEN `_load_module()` restores that prior
    entry -- it must never be left deleted or permanently clobbered by the
    half-initialized failed module."""
    module_name = "build_intake_capsule_load_module_prior_exists_test"
    sentinel = types.ModuleType(module_name)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = sentinel
    try:
        _patch_spec_from_file_location_to_fail(monkeypatch, module_name)

        try:
            mod._load_module(SCRIPT_PATH, module_name)
            raise AssertionError("expected RuntimeError to propagate")
        except RuntimeError:
            pass
        assert sys.modules.get(module_name) is sentinel
    finally:
        if previous is not None:
            sys.modules[module_name] = previous
        else:
            sys.modules.pop(module_name, None)


# ---------------------------------------------------------------------------
# Issue #3012: a trusted fingerprint-NON-ready go that is newer than a trusted
# blocked must not hide that blocked result from
# _normalize_contract_snapshot_live().  Real shared parser and real
# production consumer; only the GitHub read boundary is data.
# Timeline notation: G = fingerprint-ready trusted go, B = trusted blocked,
# P = schema-valid trusted go that is NOT fingerprint-ready.
# ---------------------------------------------------------------------------

_NR_ISSUE_URL = "https://github.com/squne121/agent-platform/issues/958"
_NR_ISSUE_BODY = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n"
_NR_UNTRUSTED = {
    "author": "mallory",
    "author_id": 4242,
    "author_type": "User",
    "author_association": "NONE",
}


def _nr_comment(cid: int, created_at: str, body: str, **identity) -> dict:
    return json.loads(_comment_ndjson(body, comment_id=cid, created_at=created_at, **identity))


def _nr_ready_go(cid: int, created_at: str, **identity) -> dict:
    body_sha256 = mod._sha256(_NR_ISSUE_BODY)
    paths_hash = mod._live_allowed_paths_hash(_NR_ISSUE_BODY)
    body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: {_NR_ISSUE_URL}
  body_sha256: "{body_sha256}"{_fingerprint_yaml_block(cid, body_sha256, paths_hash)}```
"""
    return _nr_comment(cid, created_at, body, **identity)


def _nr_nonready_go(cid: int, created_at: str, **identity) -> dict:
    body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: {_NR_ISSUE_URL}
  body_sha256: "{mod._sha256(_NR_ISSUE_BODY)}"
```
"""
    return _nr_comment(cid, created_at, body, **identity)


def _nr_blocked(cid: int, created_at: str, **identity) -> dict:
    body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: {_NR_ISSUE_URL}
```
"""
    return _nr_comment(cid, created_at, body, **identity)


def _nr_assert_fixture_shape(comments, *, go_id=None, blocked_id=None, nonready_id=None) -> None:
    """After the real parser: G/B/P are trusted, schema-valid, and P is not fingerprint-ready."""
    parsed, _counts = mod._parse_contract_results(comments, _NR_ISSUE_URL, 958)
    by_id = {r["comment_id"]: r for r in parsed}
    for cid in (go_id, blocked_id, nonready_id):
        if cid is not None:
            assert by_id[cid]["is_trusted_author"] is True
    if go_id is not None:
        assert by_id[go_id]["status"] == "go"
        assert by_id[go_id]["is_fingerprint_ready"] is True
    if blocked_id is not None:
        assert by_id[blocked_id]["status"] == "blocked"
    if nonready_id is not None:
        assert by_id[nonready_id]["status"] == "go"
        assert by_id[nonready_id]["is_fingerprint_ready"] is False


def _nr_normalize(comments: list[dict]) -> dict:
    snapshot, _complete = mod._normalize_contract_snapshot_live(
        _NR_ISSUE_URL,
        _NR_ISSUE_BODY,
        mod._sha256(_NR_ISSUE_BODY),
        "2026-06-19T00:00:00Z",
        comments,
        {},
    )
    return snapshot


def _nr_url(cid: int) -> str:
    return f"{_NR_ISSUE_URL.replace('/issues/958', '')}/issues/958#issuecomment-{cid}"


_NR_T = "2026-06-19T00:0{}:00Z"


def test_nonready_go_after_blocked_routes_to_latest_blocked_not_existing_go():
    comments = [
        _nr_ready_go(101, _NR_T.format(1)),
        _nr_blocked(102, _NR_T.format(2)),
        _nr_nonready_go(103, _NR_T.format(3)),
    ]
    _nr_assert_fixture_shape(comments, go_id=101, blocked_id=102, nonready_id=103)

    snapshot = _nr_normalize(comments)

    assert snapshot["normalized_status"] == "latest_blocked"
    assert snapshot["source"] == "latest_blocked"
    assert snapshot["upstream_status"] == "blocked"
    assert snapshot["contract_snapshot_url"] == _nr_url(102)


def test_nonready_go_after_blocked_end_to_end_capsule_routes_to_blocker_triage():
    comments = [
        _nr_ready_go(101, _NR_T.format(1)),
        _nr_blocked(102, _NR_T.format(2)),
        _nr_nonready_go(103, _NR_T.format(3)),
    ]
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(body=_NR_ISSUE_BODY), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, "\n".join(json.dumps(c) for c in comments), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/agent-platform", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "latest_blocked"
    assert capsule["contract_snapshot"]["contract_snapshot_url"] == _nr_url(102)
    assert capsule["next_action"]["route"] == "run_contract_blocker_triage"


def test_nonready_go_after_blocked_positive_controls_ready_go_still_adopted():
    only_g = [_nr_ready_go(101, _NR_T.format(1))]
    g_then_p = [_nr_ready_go(101, _NR_T.format(1)), _nr_nonready_go(103, _NR_T.format(3))]
    _nr_assert_fixture_shape(g_then_p, go_id=101, nonready_id=103)
    for comments in (only_g, g_then_p):
        snapshot = _nr_normalize(comments)
        assert snapshot["normalized_status"] == "go"
        assert snapshot["source"] == "existing_go"
        assert snapshot["contract_snapshot_url"] == _nr_url(101)


def test_nonready_go_after_blocked_later_ready_go_beats_earlier_blocked():
    comments = [
        _nr_ready_go(101, _NR_T.format(1)),
        _nr_blocked(102, _NR_T.format(2)),
        _nr_ready_go(104, _NR_T.format(4)),
    ]
    snapshot = _nr_normalize(comments)
    assert snapshot["normalized_status"] == "go"
    assert snapshot["contract_snapshot_url"] == _nr_url(104)


def test_nonready_go_after_blocked_blocked_without_ready_go_matches_blocked_alone():
    blocked_and_p = [_nr_blocked(102, _NR_T.format(2)), _nr_nonready_go(103, _NR_T.format(3))]
    _nr_assert_fixture_shape(blocked_and_p, blocked_id=102, nonready_id=103)
    for comments in (blocked_and_p, [_nr_blocked(102, _NR_T.format(2))]):
        snapshot = _nr_normalize(comments)
        assert snapshot["normalized_status"] == "latest_blocked"
        assert snapshot["source"] == "latest_blocked"
        assert snapshot["contract_snapshot_url"] == _nr_url(102)


def test_nonready_go_after_blocked_blocked_directly_after_ready_go_is_blocked():
    snapshot = _nr_normalize([_nr_ready_go(101, _NR_T.format(1)), _nr_blocked(102, _NR_T.format(2))])
    assert snapshot["normalized_status"] == "latest_blocked"
    assert snapshot["contract_snapshot_url"] == _nr_url(102)


def test_nonready_go_after_blocked_nonready_go_alone_stays_missing_go():
    snapshot = _nr_normalize([_nr_nonready_go(103, _NR_T.format(3))])
    assert snapshot["normalized_status"] == "missing_go"
    assert snapshot["contract_snapshot_url"] is None


def test_nonready_go_after_blocked_untrusted_blocked_and_plain_comments_do_not_reject():
    comments = [
        _nr_ready_go(101, _NR_T.format(1)),
        _nr_blocked(102, _NR_T.format(2), **_NR_UNTRUSTED),
        _nr_comment(105, _NR_T.format(3), "ordinary discussion", **_NR_UNTRUSTED),
        _nr_comment(106, _NR_T.format(3), "trusted but plain comment"),
    ]
    snapshot = _nr_normalize(comments)
    assert snapshot["normalized_status"] == "go"
    assert snapshot["contract_snapshot_url"] == _nr_url(101)


def test_nonready_go_after_blocked_input_order_does_not_change_result():
    comments = [
        _nr_ready_go(101, _NR_T.format(1)),
        _nr_blocked(102, _NR_T.format(2)),
        _nr_nonready_go(103, _NR_T.format(3)),
    ]
    for ordering in (list(reversed(comments)), [comments[2], comments[0], comments[1]]):
        snapshot = _nr_normalize(ordering)
        assert snapshot["normalized_status"] == "latest_blocked"
        assert snapshot["contract_snapshot_url"] == _nr_url(102)


def test_nonready_go_after_blocked_same_created_at_uses_numeric_comment_id():
    same = _NR_T.format(5)
    blocked_wins = [_nr_ready_go(101, _NR_T.format(1)), _nr_ready_go(110, same), _nr_blocked(120, same)]
    snapshot = _nr_normalize(blocked_wins)
    assert snapshot["normalized_status"] == "latest_blocked"
    assert snapshot["contract_snapshot_url"] == _nr_url(120)
    go_wins = [_nr_ready_go(101, _NR_T.format(1)), _nr_blocked(9, same), _nr_ready_go(10, same)]
    snapshot = _nr_normalize(go_wins)
    assert snapshot["normalized_status"] == "go"
    assert snapshot["contract_snapshot_url"] == _nr_url(10)


def test_nonready_go_after_blocked_fingerprint_binding_mismatch_keeps_existing_rejection():
    wrong_paths = _nr_ready_go(101, _NR_T.format(1))
    wrong_paths["body"] = wrong_paths["body"].replace(
        mod._live_allowed_paths_hash(_NR_ISSUE_BODY), "e" * 64
    )
    snapshot = _nr_normalize([wrong_paths])
    assert snapshot["normalized_status"] == "missing_go"
    assert snapshot["source"] == "fingerprint_live_allowed_paths_mismatch"
    stale_snapshot, _ = mod._normalize_contract_snapshot_live(
        _NR_ISSUE_URL, _NR_ISSUE_BODY, mod._sha256("another body"),
        "2026-06-19T00:00:00Z", [_nr_ready_go(101, _NR_T.format(1))], {},
    )
    assert stale_snapshot["normalized_status"] == "stale"
    assert stale_snapshot["source"] == "existing_go"
