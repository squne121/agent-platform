"""
test_preflight_run_with_anchor.py

Tests for the `preflight.run.with_anchor` sibling exact profile added to
command_registry.py (Issue #1498).

Covers AC1, AC2, and Positive/Negative Test Matrix items #1, #9-#14.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as reg  # noqa: E402


# ---------------------------------------------------------------------------
# AC1: preflight.run.with_anchor is a sibling exact profile; preflight.run
# itself is byte-for-byte unmodified.
# ---------------------------------------------------------------------------

# Snapshot of the exact `preflight.run` entry as it existed prior to Issue
# #1498. If this entry ever changes, this test must fail loudly (AC1) rather
# than silently pass, since the Issue's core invariant is that `preflight.run`
# is untouched by the sibling-profile addition.
_EXPECTED_PREFLIGHT_RUN_ENTRY = {
    "id": "preflight.run",
    "argv": [
        # Issue #2311 AC1: canonical bare `preflight.run` now first-hops
        # into `workflow_start_entry.py` (the workflow capability preflight
        # carrier), not `run_refinement_preflight.py` directly. Sibling
        # anchor-comment profiles (below) are unaffected and continue to
        # first-hop into `run_refinement_preflight.py`.
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
        "--issue-number", "{issue_number}",
        "--repo", "{repo}",
    ],
    "shell": False,
    "cwd_policy": "repo_root",
    "execution_class": "exact_skill_runtime",
    "required_cwd": "canonical_main_root",
    "required_branch": "default_branch",
    "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
    "network_effect": "github_read_only",
    "stdin_contract": "none",
    "stdout_contract": "refinement_preflight_result/v1",
    "timeout_seconds": 120,
    "mutation": False,
    "placeholders": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
    },
}


def test_registry_sibling_profile_preserves_preflight_run():
    """AC1: `preflight.run.with_anchor` exists as a sibling entry and
    `preflight.run` itself is unchanged (argv/placeholders/execution_class)."""
    assert "preflight.run.with_anchor" in reg.REGISTRY
    assert reg.REGISTRY["preflight.run"] == _EXPECTED_PREFLIGHT_RUN_ENTRY

    anchor_entry = reg.REGISTRY["preflight.run.with_anchor"]
    assert anchor_entry["execution_class"] == "exact_skill_runtime_anchor"
    assert anchor_entry["required_cwd"] == "canonical_main_root"
    assert anchor_entry["required_branch"] == "default_branch"
    assert anchor_entry["network_effect"] == "github_read_only"
    assert anchor_entry["mutation"] is False
    assert anchor_entry["allowed_write_roots"] == [
        ".claude/artifacts/issue-refinement-loop/{active_issue}/"
    ]
    assert anchor_entry["argv"] == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "{issue_number}",
        "--repo", "{repo}",
        "--anchor-comment-url", "{anchor_comment_url}",
    ]
    assert anchor_entry["placeholders"]["anchor_comment_url"] == {
        "type": "github_issue_comment_url",
        "required": True,
    }


def test_registry_contract_update_phase_is_explicit_and_preflight_remains_read_only():
    """#1877 AC3: the mutation consumer is a distinct registry command.

    Issue #2393: `network_effect` is `github_mutation`, not
    `github_read_only` -- both `contract_update.run.*` entries already
    declare `mutation: True` (they write to GitHub via
    `--consume-contract-patch-plan`), so `github_read_only` was factually
    wrong. Corrected in the same change that migrates this profile's child
    dispatch to the dedicated control-plane runtime.
    """
    entry = reg.REGISTRY["contract_update.run.with_human_context"]
    assert reg.REGISTRY["preflight.run.with_anchor"]["mutation"] is False
    assert reg.REGISTRY["preflight.run.with_anchor"]["network_effect"] == "github_read_only"
    assert "--consume-contract-patch-plan" not in reg.REGISTRY["preflight.run.with_anchor"]["argv"]
    assert entry["mutation"] is True
    assert entry["main_control_plane_only"] is True
    assert entry["execution_class"] == "exact_skill_runtime_contract_update_anchor"
    assert entry["required_cwd"] == "canonical_main_root"
    assert entry["required_branch"] == "default_branch"
    assert entry["network_effect"] == "github_mutation"
    assert entry["allowed_write_roots"] == [
        ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        "artifacts/{active_issue}/issue-metadata/",
    ]
    assert entry["argv"][-1] == "--consume-contract-patch-plan"

    anchor_entry = reg.REGISTRY["contract_update.run.with_anchor"]
    assert anchor_entry["mutation"] is True
    assert anchor_entry["main_control_plane_only"] is True
    assert anchor_entry["network_effect"] == "github_mutation"
    assert anchor_entry["allowed_write_roots"] == [
        ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        "artifacts/{active_issue}/issue-metadata/",
    ]

    url = "https://github.com/squne121/agent-platform/issues/1877#issuecomment-5143816923"
    assert reg.render_command(
        "contract_update.run.with_human_context",
        {"issue_number": 1877, "repo": "squne121/agent-platform", "anchor_comment_url": url},
    ) == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "1877", "--repo", "squne121/agent-platform",
        "--anchor-comment-url", url, "--human-context-comment-url", url,
        "--consume-contract-patch-plan",
    ]


def test_registry_sibling_profile_renders_argv():
    """render_command() produces the expected 10-token argv for
    preflight.run.with_anchor."""
    url = "https://github.com/squne121/agent-platform/issues/1492#issuecomment-4959671503"
    argv = reg.render_command(
        "preflight.run.with_anchor",
        {"issue_number": 1492, "repo": "squne121/agent-platform", "anchor_comment_url": url},
    )
    assert argv == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "1492",
        "--repo", "squne121/agent-platform",
        "--anchor-comment-url", url,
    ]


def test_registry_explicit_origin_profiles_do_not_infer_human_provenance():
    """P0: caller-selected lane, not anchor presence, determines origin."""
    url = "https://github.com/squne121/agent-platform/issues/1492#issuecomment-4959671503"
    values = {"issue_number": 1492, "repo": "squne121/agent-platform", "anchor_comment_url": url}

    generic = reg.render_command("preflight.run.with_anchor", values)
    human = reg.render_command("preflight.run.with_human_context", values)
    agent = reg.render_command("preflight.run.with_agent_report", values)

    assert "--human-context-comment-url" not in generic
    assert human[-2:] == ["--human-context-comment-url", url]
    assert agent[-2:] == ["--agent-report-comment-url", url]
    assert "contract_update.run.with_agent_report" not in reg.REGISTRY


def test_registry_sibling_profile_missing_anchor_raises():
    """render_command() fails closed when the required anchor_comment_url
    placeholder is missing."""
    with pytest.raises(ValueError):
        reg.render_command(
            "preflight.run.with_anchor",
            {"issue_number": 1492, "repo": "squne121/agent-platform"},
        )


# ---------------------------------------------------------------------------
# AC2: github_issue_comment_url placeholder type — Positive/Negative Test
# Matrix #9-#14.
# ---------------------------------------------------------------------------

_VALID_URL = "https://github.com/squne121/agent-platform/issues/1492#issuecomment-4959671503"

_NEGATIVE_URLS = {
    # Matrix #9: pull request review comment URL, not an issue comment URL.
    "pull_request_review_comment": (
        "https://github.com/squne121/agent-platform/pull/1492/files#r1234567"
    ),
    # Matrix #10: discussion_r fragment form (PR review comment fragment).
    "discussion_r_fragment": (
        "https://github.com/squne121/agent-platform/issues/1492#discussion_r1234567"
    ),
    # Matrix #11: query string present.
    "query_string": (
        "https://github.com/squne121/agent-platform/issues/1492?tab=timeline"
        "#issuecomment-4959671503"
    ),
    # Matrix #12: extra fragment / suffix / trailing slash.
    "trailing_slash": (
        "https://github.com/squne121/agent-platform/issues/1492#issuecomment-4959671503/"
    ),
    "extra_suffix": (
        "https://github.com/squne121/agent-platform/issues/1492#issuecomment-4959671503-extra"
    ),
    # Matrix #13: userinfo, port, non-GitHub host, HTTP scheme.
    "userinfo": (
        "https://user:pass@github.com/squne121/agent-platform/issues/1492"
        "#issuecomment-4959671503"
    ),
    "port": (
        "https://github.com:8443/squne121/agent-platform/issues/1492"
        "#issuecomment-4959671503"
    ),
    "non_github_host": (
        "https://evil.example.com/squne121/agent-platform/issues/1492"
        "#issuecomment-4959671503"
    ),
    "subdomain_host": (
        "https://gist.github.com/squne121/agent-platform/issues/1492"
        "#issuecomment-4959671503"
    ),
    "http_scheme": (
        "http://github.com/squne121/agent-platform/issues/1492#issuecomment-4959671503"
    ),
    # Matrix #14: percent-encoding disguise of canonical shape.
    "percent_encoded_hash": (
        "https://github.com/squne121/agent-platform/issues/1492%23issuecomment-4959671503"
    ),
    "percent_encoded_dotdot": (
        "https://github.com/squne121/agent-platform/%2e%2e/issues/1492"
        "#issuecomment-4959671503"
    ),
    # Not a URL at all / empty
    "empty": "",
    "not_a_url": "not-a-url",
}


class TestGithubIssueCommentUrlPlaceholderType:
    def test_valid_url_accepted(self):
        argv = reg.render_command(
            "preflight.run.with_anchor",
            {
                "issue_number": 1492,
                "repo": "squne121/agent-platform",
                "anchor_comment_url": _VALID_URL,
            },
        )
        assert _VALID_URL in argv

    @pytest.mark.parametrize("name", sorted(_NEGATIVE_URLS.keys()))
    def test_negative_matrix_rejected(self, name: str):
        url = _NEGATIVE_URLS[name]
        with pytest.raises(ValueError):
            reg.render_command(
                "preflight.run.with_anchor",
                {
                    "issue_number": 1492,
                    "repo": "squne121/agent-platform",
                    "anchor_comment_url": url,
                },
            )


def test_github_issue_comment_url_type_rejects_negative_matrix():
    """AC2 entrypoint test referenced by the Issue's Verification Commands."""
    for url in _NEGATIVE_URLS.values():
        with pytest.raises(ValueError):
            reg.render_command(
                "preflight.run.with_anchor",
                {
                    "issue_number": 1492,
                    "repo": "squne121/agent-platform",
                    "anchor_comment_url": url,
                },
            )


def test_registry_export_is_json_serializable_with_anchor_entry():
    """export_registry() (used by --list) does not choke on the new entry."""
    import json

    data = reg.export_registry()
    assert "preflight.run.with_anchor" in data["commands"]
    json.dumps(data)  # must not raise


def test_registry_entry_is_a_deep_copy_safe_snapshot():
    """Sanity: mutating a returned export dict must not corrupt REGISTRY."""
    data = reg.export_registry()
    mutated = copy.deepcopy(data)
    mutated["commands"]["preflight.run.with_anchor"]["argv"] = ["tampered"]
    assert reg.REGISTRY["preflight.run.with_anchor"]["argv"] != ["tampered"]



# ---------------------------------------------------------------------------
# #2048 Scope Delta: production-path E2E for the approved-scope-reframe /
# empty-operations[] router.
#
# Unlike test_scope_only_reframe_route.py (which drives
# decide_scope_reframe_contract_route() / run_trusted_anchor_iteration_zero()
# directly), these tests exercise the ACTUAL canonical production entry
# points:
#   - the positive/replay cases below call run_preflight() itself (the same
#     function `contract_update.run.with_human_context` invokes via
#     `--consume-contract-patch-plan`), through fixture mode with injected
#     GitHub-mutation callbacks (no live gh calls, no real Issue write).
#   - the negative regression cases call
#     consume_trusted_anchor_contract_patch_plan() directly (the exact
#     production consumer function -- not a copy/reimplementation) with
#     crafted known_context/patch_plan/anchor inputs, since exercising each
#     failure mode through the full planner subprocess boundary would
#     require a distinct planner-triggering fixture per case without adding
#     assurance beyond what the consumer-boundary call already proves.
#
# This directly targets the #2048 PR review blocker (iteration 1/2):
# decide_scope_reframe_contract_route() was reachable only from unit tests,
# never from the real `consume_trusted_anchor_contract_patch_plan()` /
# `run_preflight()` call chain, because `known_context["scope_delta_decision"]`
# was never threaded into `run_trusted_anchor_iteration_zero()`.
# ---------------------------------------------------------------------------

_E2E_SKILL_ROOT = Path(__file__).resolve().parent.parent
_E2E_SCRIPTS_DIR = _E2E_SKILL_ROOT / "scripts"


def _e2e_load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _E2E_SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_e2e_preflight = _e2e_load_module("run_refinement_preflight_2048_e2e", "run_refinement_preflight.py")

_E2E_REPO = "squne121/agent-platform"
_E2E_ISSUE = 2048
_E2E_URL = f"https://github.com/{_E2E_REPO}/issues/{_E2E_ISSUE}#issuecomment-2048099"
_E2E_DELTA = "docs/product/features/scope-only-reframe.md"


def _e2e_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _e2e_anchor_body(delta: str = _E2E_DELTA) -> str:
    return (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        f"target:\n  repo: {_E2E_REPO}\n  issue_number: {_E2E_ISSUE}\n"
        "decision: approve_scope_delta\n"
        f'allowed_path_deltas: ["{delta}"]\n'
        'rationale: "approved scope reframe for #2048 E2E fixture"\n'
        'required_rerun: ["contract_review"]\n'
        "```\n"
    )


def _e2e_issue_body(*, allowed_paths_includes_delta: bool) -> str:
    allowed_paths_lines = ["- docs/product/features/existing.md"]
    if allowed_paths_includes_delta:
        allowed_paths_lines.append(f"- {_E2E_DELTA}")
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: implementation\n"
        "parent_issue: none\n"
        "goal_ref: test\n"
        "change_kind: workflow\n"
        "```\n\n"
        "## Parent Issue\n\nnone\n\n"
        "## Parent Goal Ref\n\ntest\n\n"
        "## Current Validated Scope\n\n- test\n\n"
        "## Remaining Parent Gaps\n\nnone\n\n"
        "## Outcome\n\ntest\n\n"
        "## In Scope\n\n- test\n\n"
        "## Out of Scope\n\n- none\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: test\n\n"
        "## Verification Commands\n\n```bash\n$ true\n```\n\n"
        "## Allowed Paths\n\n" + "\n".join(allowed_paths_lines) + "\n\n"
        "## Stop Conditions\n\n- none\n\n"
        "## Required Skills\n\n- none\n"
    )


def _e2e_anchor_comment(*, delta: str = _E2E_DELTA, comment_id: int = 2048099) -> dict:
    return {
        "id": comment_id,
        "body": _e2e_anchor_body(delta),
        "issue_url": f"https://api.github.com/repos/{_E2E_REPO}/issues/{_E2E_ISSUE}",
        "created_at": "2026-08-09T00:00:00Z",
        "updated_at": "2026-08-09T00:00:00Z",
        "html_url": _E2E_URL,
        "url": f"https://api.github.com/repos/{_E2E_REPO}/issues/comments/{comment_id}",
        "user": {"login": "squne121", "type": "User"},
        "author_association": "OWNER",
    }


def _e2e_callbacks(*, issue_body: str, anchor_comment: dict):
    calls = {"apply_transaction": 0, "fresh_checks": 0}
    state = {"body": issue_body}

    def fetch_current():
        return (
            {"body": state["body"], "updatedAt": "2026-08-09T00:00:00Z"},
            dict(anchor_comment, html_url=_E2E_URL),
        )

    def candidate_readiness(_body):
        return {
            "status": "go",
            "body_sha256": "sha256:candidate",
            "source_checks": [],
            "errors": [],
            "readiness_result_ref": "fixture",
        }

    def apply_transaction(current_issue, candidate_body, readiness):
        calls["apply_transaction"] += 1
        state["body"] = candidate_body
        return {"status": "applied"}

    def fresh_checks(_current_issue):
        calls["fresh_checks"] += 1
        return {
            "preflight": "unavailable",
            "review": "unavailable",
            "readiness": "unavailable",
            "allowed_paths": "unavailable",
            "permission_profile": "unavailable",
            "runtime_evidence": "unavailable",
        }

    return {
        "fetch_current": fetch_current,
        "candidate_readiness": candidate_readiness,
        "apply_transaction": apply_transaction,
        "fresh_checks": fresh_checks,
    }, calls


def _e2e_run_preflight(tmp_path, *, allowed_paths_includes_delta: bool, run_id: str):
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=allowed_paths_includes_delta)
    anchor_comment = _e2e_anchor_comment()
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": _E2E_ISSUE,
        "repo": _E2E_REPO,
        "now": "2026-08-09T00:00:00Z",
        "issue": {"number": _E2E_ISSUE, "title": "test", "body": issue_body, "labels": []},
        "comments": [],
        "anchor_comment_urls": [_E2E_URL],
        "anchor_comments": [anchor_comment],
    }
    fixture_path = tmp_path / f"preflight_{run_id}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    callbacks, calls = _e2e_callbacks(issue_body=issue_body, anchor_comment=anchor_comment)
    artifact_dir = (
        _E2E_SKILL_ROOT.parent.parent / "artifacts" / "issue-refinement-loop" / str(_E2E_ISSUE)
    )
    try:
        result, exit_code = _e2e_preflight.run_preflight(
            issue_number=_E2E_ISSUE,
            repo=_E2E_REPO,
            anchor_comment_urls=[_E2E_URL],
            fixture_path=fixture_path,
            consume_contract_patch_plan=True,
            contract_update_callbacks=callbacks,
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
    return result, exit_code, calls


def test_e2e_full_rewrite_required_reaches_next_action_via_canonical_consumer(tmp_path, capsys):
    """AC1/AC6 positive fixture: an approved trusted-anchor scope reframe
    (non-empty allowed_path_deltas) not yet reflected in the current Issue
    body, with no section-bound operations[] derivable, reaches
    NEXT_ACTION: issue_editor_required on stdout AND result["next_action"]
    from the REAL run_preflight() -> consume_trusted_anchor_contract_patch_plan()
    call chain -- not a unit-level shortcut."""
    result, exit_code, calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=False, run_id="positive"
    )
    captured = capsys.readouterr()

    assert "NEXT_ACTION: issue_editor_required" in captured.out
    assert result["next_action"] == "issue_editor_required"
    # AC2: no contract-update mutation was ever attempted.
    assert calls["apply_transaction"] == 0
    assert result["contract_update"]["writes"] == 0
    # AC3: no separate scope-reframe comment publisher exists in this call
    # chain at all -- there is nothing to assert a count of 0 against beyond
    # the absence of any comment-posting callback, which this fixture never
    # supplies.
    assert result["contract_update"]["status"] != "no_change"
    assert result["contract_update"]["reason_code"] == "approved_scope_requires_full_contract_rewrite"
    # AC4: the existing _bounded_contract_update_handoff() post-update
    # 6-gate (preflight/review/readiness/allowed_paths/permission_profile/
    # runtime_evidence) is still exercised for this disposition -- fresh_checks
    # is invoked exactly once, the same reused gate #1877 already established,
    # not a duplicate routing plane.
    assert calls["fresh_checks"] == 1


def test_e2e_full_rewrite_required_replay_is_deterministic_and_side_effect_free(tmp_path):
    """AC2/AC3/regression #7: replaying the identical disposition (same
    anchor, same unreflected body) through run_preflight() twice never
    attempts a mutation and always reaches the same route."""
    first_result, _, first_calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=False, run_id="replay_a"
    )
    second_result, _, second_calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=False, run_id="replay_b"
    )

    assert first_result["next_action"] == "issue_editor_required"
    assert second_result["next_action"] == "issue_editor_required"
    assert first_calls["apply_transaction"] == 0
    assert second_calls["apply_transaction"] == 0


def test_e2e_deltas_already_reflected_is_proven_no_change_not_issue_editor_required(tmp_path):
    """Regression #3: when the approved allowed_path_deltas are ALREADY
    present in the current Issue body's Allowed Paths section, this is
    `proven_no_change` -- never promoted to issue_editor_required."""
    result, _exit_code, calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=True, run_id="already_reflected"
    )

    assert result["next_action"] != "issue_editor_required"
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# Negative regressions #1-#6: direct calls to the production consumer
# function consume_trusted_anchor_contract_patch_plan() (not a
# reimplementation) with crafted known_context / patch_plan / anchor
# identity inputs.
# ---------------------------------------------------------------------------


def _e2e_consumer_kwargs(*, patch_plan: dict, known_context: "dict | None", issue_body: str):
    anchor_comment = _e2e_anchor_comment()
    anchor_body = anchor_comment["body"]
    callbacks, calls = _e2e_callbacks(issue_body=issue_body, anchor_comment=anchor_comment)
    return (
        dict(
            repo=_E2E_REPO,
            issue_number=_E2E_ISSUE,
            issue={"body": issue_body, "updatedAt": "2026-08-09T00:00:00Z"},
            anchor_url=_E2E_URL,
            anchor_payload=anchor_comment,
            anchor_body=anchor_body,
            contract_patch_plan=patch_plan,
            callbacks=callbacks,
            known_context=known_context,
        ),
        calls,
        anchor_body,
    )


def _e2e_approved_scope_delta_decision(*, anchor_body: str, delta: str = _E2E_DELTA) -> dict:
    return {
        "status": "approved_by_trusted_anchor",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
        "anchor_comment_url": _E2E_URL,
        "anchor_comment_hash": _e2e_sha256(anchor_body),
        "allowed_path_deltas": [delta],
        "required_rerun": ["contract_review"],
    }


def test_negative_1_non_empty_operations_preserves_ordinary_contract_update_behavior():
    """Regression #1: non-empty operations[] still take the ordinary
    section-bound apply_transaction path, unaffected by the #2048 wiring."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    operations = [
        {
            "section": "Allowed Paths",
            "op": "append",
            "text": f"- `{_E2E_DELTA}`",
            "rationale": "test",
            "source_evidence_index": 0,
        }
    ]
    patch_plan = {
        "schema_version": "CONTRACT_PATCH_PLAN_V1",
        "target_issue_number": _E2E_ISSUE,
        "base_issue_body_sha256": _e2e_sha256(issue_body),
        "source_evidence": [],
        "operations": operations,
        "forbidden": ["direct_github_write", "implementation_phase_transition"],
        "required_next_step": "rerun_refinement_after_contract_update",
    }
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "applied"
    assert calls["apply_transaction"] == 1
    assert "rewrite_route" not in result


def test_negative_2_unapproved_scope_reframe_does_not_escalate():
    """Regression #2: scope_delta_decision.status != approved_by_trusted_anchor
    -- empty operations[] stays an ordinary no_change, never
    issue_editor_required."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "status": "fail_closed",
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_3_already_reflected_deltas_are_proven_no_change():
    """Regression #3 (consumer-boundary level): allowed_path_deltas already
    present in the current body -- proven_no_change, not escalated."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=True)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_4_malformed_operations_type_fails_closed_without_coercion():
    """Regression #4: operations is not a list -- fails closed immediately,
    never coerced to []; run_trusted_anchor_iteration_zero is never reached."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": "not-a-list"}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "blocked"
    assert result["writes"] == 0
    assert calls["apply_transaction"] == 0
    assert calls["fresh_checks"] == 0


def test_negative_5_malformed_allowed_path_deltas_fails_closed():
    """Regression #5: an explicit EMPTY allowed_path_deltas list is not a
    scope-reframe signal at all (consistent with
    `classify_scope_reframe_disposition()`'s own
    `bool(normalized_deltas)` gate) -- fails closed to the ordinary
    no_change path, never escalated to issue_editor_required, and never
    silently promoted to `full_rewrite_required`. See
    `test_negative_5b_non_list_allowed_path_deltas_is_invalid` for the
    genuinely malformed (non-list) case, which IS `invalid`."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "allowed_path_deltas": [],
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_5b_non_list_allowed_path_deltas_is_invalid():
    """Regression #5 (PR #2057 OWNER review P0-1): a NON-LIST
    allowed_path_deltas (genuinely malformed, distinct from an explicit
    empty list) is a DISTINCT `invalid` disposition/status -- never
    silently collapsed into the same `no_change` observable as a
    genuinely satisfied `proven_no_change`."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "allowed_path_deltas": "not-a-list",
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "invalid"
    assert result["disposition"]["disposition"] == "invalid"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_6_anchor_identity_binding_drift_fails_closed():
    """Regression #6 (revised, PR #2057 OWNER review P0-1):
    scope_delta_decision.anchor_comment_url does not match THIS consumer
    call's anchor_url (a stale/different decision) -- a distinct `invalid`
    outcome, never silently the same as an ordinary no-op."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "anchor_comment_url": _E2E_URL + "-different",
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "invalid"
    assert result["disposition"]["disposition"] == "invalid"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_6b_anchor_body_hash_drift_fails_closed():
    """Regression #6 (body hash variant, revised): scope_delta_decision.
    anchor_comment_hash does not match sha256(this call's anchor_body) --
    a distinct `invalid` outcome (TOCTOU-adjacent: the anchor was edited
    after the decision was computed)."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "anchor_comment_hash": "0" * 64,
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "invalid"
    assert result["disposition"]["disposition"] == "invalid"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# PR #2057 OWNER REQUEST_CHANGES (iteration 4, blocker 3 / P0-1 residual):
# `patch_plan_producer_available` -- the classify_scope_reframe_disposition()
# kwarg that marks "the freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 producer
# could not produce a schema-valid patch plan for this evidence" -- must be
# reachable from the REAL production consumer
# consume_trusted_anchor_contract_patch_plan() (not only exercised as a pure
# decide_rewrite_route.classify_scope_reframe_disposition() unit call).
# ---------------------------------------------------------------------------


def test_producer_unavailable_reaches_invalid_disposition_via_production_consumer():
    """Blocker 3: patch_plan_producer_available=False, threaded through the
    real consume_trusted_anchor_contract_patch_plan() ->
    run_trusted_anchor_iteration_zero() call chain, must classify an
    otherwise-approved empty-operations scope reframe as `invalid`
    (REASON_INVALID_PATCH_PLAN_PRODUCER_UNAVAILABLE) instead of silently
    accepting the synthesized empty-operations plan as
    `full_rewrite_required`. No mutation is ever attempted."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(
        **kwargs, patch_plan_producer_available=False
    )

    assert result["status"] == "invalid"
    assert result["disposition"]["disposition"] == "invalid"
    assert result["disposition"]["reason_code"] == "invalid_patch_plan_producer_unavailable"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0
    assert result.get("writes", 0) == 0


def test_producer_available_default_true_preserves_full_rewrite_required():
    """Contrast case: the SAME inputs as the previous test, but with
    `patch_plan_producer_available` left at its default (`True`), must
    still reach `full_rewrite_required` -- proving the new kwarg is a
    strictly additive fail-closed gate, never a behavior change for the
    pre-existing positive fixture."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert result["rewrite_route"]["disposition"] == "full_rewrite_required"
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# PR #2057 OWNER REQUEST_CHANGES (Warning, not blocker): explicit anchor /
# Issue-body TOCTOU regression coverage -- fetch_current() (the FRESH
# re-read performed immediately before disposition classification) returns
# a value that differs from the pre-fetch snapshot the caller classified
# the structured ANCHOR_SCOPE_REFRAME_V1 decision against.
# ---------------------------------------------------------------------------


def test_toctou_anchor_edited_between_classification_and_fresh_fetch_fails_closed():
    """Anchor TOCTOU: the anchor comment used to classify
    `scope_delta_decision` differs (body edited -- a different
    `source_body_sha256`) from what `fetch_current()` returns at the moment
    of disposition classification. Must fail closed to
    `anchor_identity_or_trust_changed`, never silently proceed as if the
    pre-fetch anchor were still current."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    edited_anchor_body = anchor_comment["body"] + "\nedited-after-classification\n"

    def _drifted_fetch_current():
        edited_anchor = dict(anchor_comment, html_url=_E2E_URL, body=edited_anchor_body)
        return {"body": issue_body, "updatedAt": "2026-08-09T00:00:00Z"}, edited_anchor

    kwargs["callbacks"]["fetch_current"] = _drifted_fetch_current
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "blocked"
    assert result.get("failure") == "anchor_identity_or_trust_changed"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_toctou_issue_body_already_reflects_delta_at_fresh_fetch_becomes_proven_no_change():
    """Issue-body TOCTOU: the pre-fetch `issue` snapshot does NOT yet
    reflect the approved `allowed_path_deltas`, but a concurrent write
    landed the delta before `fetch_current()` re-reads the Issue body.
    The FRESH body (never the pre-fetch snapshot) must be reclassified as
    `proven_no_change`, not `full_rewrite_required` -- avoiding a redundant
    issue-editor handoff for a change that has already taken effect."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    fresh_issue_body = _e2e_issue_body(allowed_paths_includes_delta=True)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )

    def _reflects_delta_fetch_current():
        return (
            {"body": fresh_issue_body, "updatedAt": "2026-08-09T00:00:01Z"},
            dict(anchor_comment, html_url=_E2E_URL),
        )

    kwargs["callbacks"]["fetch_current"] = _reflects_delta_fetch_current
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# PR #2057 OWNER REQUEST_CHANGES (P1-5): tri-state Allowed Paths reflected
# check -- false-positive resistance regression coverage for
# `_check_scope_reframe_deltas_reflected()` (formerly `_scope_reframe_
# deltas_already_reflected()`, which had a `normalized_delta in
# current_body` whole-body substring fallback that this function removes).
# ---------------------------------------------------------------------------


def _reflected_issue_body(allowed_paths_lines: list[str], *, extra_sections: str = "") -> str:
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n"
        "parent_issue: none\ngoal_ref: test\nchange_kind: workflow\n```\n\n"
        "## Outcome\n\n" + extra_sections + "\n\n"
        "## Allowed Paths\n\n" + "\n".join(allowed_paths_lines) + "\n\n"
        "## Stop Conditions\n\n- none\n"
    )


class TestScopeReframeDeltasReflectedFalsePositiveResistance:
    def test_delta_only_in_outcome_prose_is_absent_not_present(self):
        """A delta literal appearing only in `## Outcome` prose (not the
        canonical `## Allowed Paths` section) must NOT be treated as
        reflected."""
        body = _reflected_issue_body(
            ["- docs/product/features/existing.md"],
            extra_sections="この Issue は docs/product/features/scope-only-reframe.md を扱う。",
        )
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body,
            allowed_path_deltas=["docs/product/features/scope-only-reframe.md"],
        )
        assert status == "absent"

    def test_delta_only_in_fenced_code_is_absent_not_present(self):
        body = _reflected_issue_body(
            ["- docs/product/features/existing.md"],
            extra_sections="```text\ndocs/product/features/scope-only-reframe.md\n```",
        )
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body,
            allowed_path_deltas=["docs/product/features/scope-only-reframe.md"],
        )
        assert status == "absent"

    def test_delta_as_prefix_of_a_longer_unrelated_path_is_absent(self):
        """`docs/foo` must not be matched by `docs/foobar` appearing in the
        Allowed Paths section."""
        body = _reflected_issue_body(["- docs/foobar.md"])
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body, allowed_path_deltas=["docs/foo.md"]
        )
        assert status == "absent"

    def test_delta_exactly_present_in_allowed_paths_section_is_present(self):
        body = _reflected_issue_body(
            ["- docs/product/features/existing.md", "- docs/product/features/scope-only-reframe.md"]
        )
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body,
            allowed_path_deltas=["docs/product/features/scope-only-reframe.md"],
        )
        assert status == "present"

    def test_whitespace_only_delta_entry_is_absent_never_present(self):
        """A whitespace-only delta normalizes to an empty string; it must
        never match (the pre-fix bug: an empty string is a substring of
        every body, so this always "matched")."""
        body = _reflected_issue_body(["- docs/product/features/existing.md"])
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body, allowed_path_deltas=["   "]
        )
        assert status == "absent"

    def test_empty_deltas_list_is_invalid_or_unavailable(self):
        body = _reflected_issue_body(["- docs/product/features/existing.md"])
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body, allowed_path_deltas=[]
        )
        assert status == "invalid_or_unavailable"


# ---------------------------------------------------------------------------
# #2620 AC1/AC2/AC3/AC4/AC6: explicit trusted human_review_directive (a
# FREEFORM with_human_context comment, not a structured
# ANCHOR_SCOPE_REFRAME_V1 payload) whose derived operations[] is empty
# reaches NEXT_ACTION: issue_editor_required through the real,
# production-reachable run_preflight() -> consume_trusted_anchor_
# contract_patch_plan() call chain -- never a unit-level shortcut. See
# test_decide_rewrite_route_human_review_directive.py for the SSOT-level
# and direct-consumer-level unit coverage of the same eligibility.
# ---------------------------------------------------------------------------

_HRD_ISSUE = 2620
_HRD_COMMENT_ID = 2620099
_HRD_URL = f"https://github.com/{_E2E_REPO}/issues/{_HRD_ISSUE}#issuecomment-{_HRD_COMMENT_ID}"
_HRD_EXPLICIT_BODY = (
    "Please restructure the onboarding walkthrough narrative so new "
    "contributors are not dropped mid-flow.\n\n"
    "- Please restructure the onboarding walkthrough narrative to add "
    "clarifying context for new contributors.\n"
)
_HRD_PROSE_ONLY_BODY = "This section could probably be clearer at some point."
# PR #2732 OWNER REQUEST_CHANGES (P1, iteration N): a representative
# reproduction of #2715's own origin comment
# (https://github.com/squne121/agent-platform/issues/2715#issuecomment-5770013860),
# which OWNER pointed out `_HRD_EXPLICIT_BODY` (a short freeform onboarding
# sentence) does not structurally represent -- specifically its "Allowed
# Paths:" heading followed by exact repository-relative path bullets. Kept
# verbatim to the load-bearing structural section of that comment (Title /
# change_kind / In Scope / Allowed Paths headings + bullet lists).
_HRD_2715_ORIGIN_BODY = (
    "### 推奨する最小 reframe\n\n"
    "Title:\n"
    "実装: since-last full-analysis のCLI helpと前提条件を実際の起動契約に一致させる\n\n"
    "change_kind:\n"
    "code\n\n"
    "In Scope:\n"
    "- --since-last-retrospective help の条件付き説明を修正\n"
    "- --enable-full-analysis help に --publish-authorized 必須を明記\n"
    "- --publish-authorized help に analysis attempt gate でもあることを明記\n"
    "- --enable-full-analysis && !--publish-authorized を parser.error で拒否\n"
    "- help/conditional validation の regression test を test_run_retrospective.py に1件追加\n\n"
    "Allowed Paths:\n"
    "- .claude/skills/agent-retrospective/scripts/run_retrospective.py\n"
    "- .claude/skills/agent-retrospective/scripts/tests/test_run_retrospective.py\n"
)
_HRD_INVALID_BINDING_BODY = (
    "```yaml\n"
    "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
    "target:\n  repo: squne121/agent-platform\n  issue_number: 9999\n"
    "decision: approve_scope_delta\n"
    'allowed_path_deltas: ["docs/product/features/binding.md"]\n'
    'rationale: "wrong target fixture"\n'
    'required_rerun: ["contract_review"]\n'
    "```\n\n"
    + _HRD_EXPLICIT_BODY
)


def _hrd_issue_body() -> str:
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: implementation\n"
        "parent_issue: none\n"
        "goal_ref: test\n"
        "change_kind: workflow\n"
        "```\n\n"
        "## Parent Issue\n\nnone\n\n"
        "## Parent Goal Ref\n\ntest\n\n"
        "## Current Validated Scope\n\n- test\n\n"
        "## Remaining Parent Gaps\n\nnone\n\n"
        "## Outcome\n\ntest\n\n"
        "## In Scope\n\n- test\n\n"
        "## Out of Scope\n\n- none\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: test\n\n"
        "## Verification Commands\n\n```bash\n$ true\n```\n\n"
        "## Allowed Paths\n\n- docs/product/features/existing.md\n\n"
        "## Stop Conditions\n\n- none\n\n"
        "## Required Skills\n\n- none\n\n"
        "## Runtime Verification Applicability\n\n"
        "- decision: not_applicable\n"
        "- reason: static verification only for this fixture\n"
    )


def _hrd_anchor_comment(
    *, body: str, author_association: str = "OWNER", comment_id: int = _HRD_COMMENT_ID, url: str = _HRD_URL
) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "issue_url": f"https://api.github.com/repos/{_E2E_REPO}/issues/{_HRD_ISSUE}",
        "created_at": "2026-09-10T00:00:00Z",
        "updated_at": "2026-09-10T00:00:00Z",
        "html_url": url,
        "url": f"https://api.github.com/repos/{_E2E_REPO}/issues/comments/{comment_id}",
        "user": {"login": "squne121", "type": "User"},
        "author_association": author_association,
    }


def _hrd_run_preflight(
    tmp_path,
    *,
    anchor_body: str,
    run_id: str,
    author_association: str = "OWNER",
    human_context_comment_urls=(_HRD_URL,),
    comment_id: int = _HRD_COMMENT_ID,
    url: str = _HRD_URL,
    known_context_extra: "dict | None" = None,
    fresh_anchor_body: "str | None" = None,
):
    issue_body = _hrd_issue_body()
    anchor_comment = _hrd_anchor_comment(
        body=anchor_body, author_association=author_association, comment_id=comment_id, url=url
    )
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": _HRD_ISSUE,
        "repo": _E2E_REPO,
        "now": "2026-09-10T00:00:00Z",
        "issue": {"number": _HRD_ISSUE, "title": "test", "body": issue_body, "labels": []},
        "comments": [],
        "anchor_comment_urls": [url],
        "anchor_comments": [anchor_comment],
    }
    fixture_path = tmp_path / f"preflight_hrd_{run_id}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    calls = {"apply_transaction": 0, "fresh_checks": 0, "fetch_current": 0}
    state = {"body": issue_body}
    # PR #2623 review fix (P1 finding 2): `fresh_anchor_body` lets a caller
    # simulate a concurrent anchor edit between evidence capture and the
    # TOCTOU-safe fresh readback `_decide_human_review_directive_editor_
    # route()` now performs -- defaults to the SAME `anchor_body` (no
    # drift), matching every pre-existing caller of this helper byte-for-
    # byte.
    fresh_body = fresh_anchor_body if fresh_anchor_body is not None else anchor_body

    def fetch_current():
        calls["fetch_current"] += 1
        return (
            {"body": state["body"], "updatedAt": "2026-09-10T00:00:00Z"},
            dict(anchor_comment, html_url=url, body=fresh_body),
        )

    def candidate_readiness(_body):
        return {
            "status": "go",
            "body_sha256": "sha256:candidate",
            "source_checks": [],
            "errors": [],
            "readiness_result_ref": "fixture",
        }

    def apply_transaction(current_issue, candidate_body, readiness):
        calls["apply_transaction"] += 1
        state["body"] = candidate_body
        return {"status": "applied"}

    def fresh_checks(_current_issue):
        calls["fresh_checks"] += 1
        return {
            "preflight": "unavailable",
            "review": "unavailable",
            "readiness": "unavailable",
            "allowed_paths": "unavailable",
            "permission_profile": "unavailable",
            "runtime_evidence": "unavailable",
        }

    callbacks = {
        "fetch_current": fetch_current,
        "candidate_readiness": candidate_readiness,
        "apply_transaction": apply_transaction,
        "fresh_checks": fresh_checks,
    }
    known_context = {"human_context_comment_urls": list(human_context_comment_urls)}
    if known_context_extra:
        known_context.update(known_context_extra)
    artifact_dir = _E2E_SKILL_ROOT.parent.parent / "artifacts" / "issue-refinement-loop" / str(_HRD_ISSUE)
    try:
        result, exit_code = _e2e_preflight.run_preflight(
            issue_number=_HRD_ISSUE,
            repo=_E2E_REPO,
            anchor_comment_urls=[url],
            fixture_path=fixture_path,
            known_context=known_context,
            consume_contract_patch_plan=True,
            contract_update_callbacks=callbacks,
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
    return result, exit_code, calls


def test_mixed_section_directive_bound_before_real_preflight_consumer(tmp_path, monkeypatch, capsys):
    """Planner evidence-only partial plan cannot authorize a mixed H2 write."""
    comment = ("## Stop Conditions\n- Stop if tests fail.\n"
               "- Allowed Paths に `src/new.ts` を追加してください。\n")
    original_invoke = _e2e_preflight._invoke_planner
    original_consume = _e2e_preflight.consume_trusted_anchor_contract_patch_plan
    observed = {}

    def invoke(*args, **kwargs):
        plan, code, stderr, stdout = original_invoke(*args, **kwargs)
        if isinstance(plan, dict):
            observed["pre_bind"] = copy.deepcopy(plan.get("scope_signal_guard_decision_v2"))
        return plan, code, stderr, stdout

    def consume(**kwargs):
        observed["consumer_operations"] = kwargs["contract_patch_plan"]["operations"]
        return original_consume(**kwargs)

    monkeypatch.setattr(_e2e_preflight, "_invoke_planner", invoke)
    monkeypatch.setattr(_e2e_preflight, "consume_trusted_anchor_contract_patch_plan", consume)
    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path, anchor_body=comment, run_id="mixed_h2_bound",
    )
    captured = capsys.readouterr()
    assert observed["pre_bind"]["scope_delta_authority"]["contract_patch_plan"]["operations"]
    assert observed["consumer_operations"] == []
    assert result["contract_update"]["reason_code"] == "unsafe_unstructured_patch_operation"
    assert result["contract_update"]["writes"] == 0 and calls["apply_transaction"] == 0
    assert "PLANNER_FAIL_CLOSED" not in result["blockers"]
    assert "CONTRACT_UPDATE_FAILED" in result["blockers"]
    assert comment not in captured.out


def test_ac1_ac6_explicit_human_review_directive_reaches_next_action_production_reachable(tmp_path, capsys):
    """AC1/AC6: an explicit trusted human_review_directive (freeform
    with_human_context comment, NOT a structured ANCHOR_SCOPE_REFRAME_V1
    payload) whose derived operations[] is empty reaches
    NEXT_ACTION: issue_editor_required through the REAL run_preflight() ->
    consume_trusted_anchor_contract_patch_plan() call chain, with writes=0
    (mutation count 0) -- production-reachable, not a helper-only
    shortcut (AC6: a result reached only via a fallback path is FAIL, not
    PASS)."""
    result, _exit_code, calls = _hrd_run_preflight(tmp_path, anchor_body=_HRD_EXPLICIT_BODY, run_id="positive")
    captured = capsys.readouterr()

    assert "NEXT_ACTION: issue_editor_required" in captured.out
    assert result["next_action"] == "issue_editor_required"
    assert result["contract_update"]["writes"] == 0
    assert calls["apply_transaction"] == 0
    # PR #2623 review fix (P1 finding 2): the fresh, TOCTOU-safe readback
    # genuinely ran -- this route is never authorized from a stale in-hand
    # snapshot alone.
    assert calls["fetch_current"] >= 1


_MISSING_PLANNER_PATCH_PLAN = object()


def _force_hrd_planner_patch_plan_missing(monkeypatch, replacement=_MISSING_PLANNER_PATCH_PLAN):
    """Keep the real entrypoint while supplying the #2724 missing-plan shape."""
    original_invoke = _e2e_preflight._invoke_planner
    observed = {}

    def invoke_without_patch_plan(*args, **kwargs):
        plan, exit_code, stderr, stdout = original_invoke(*args, **kwargs)
        assert isinstance(plan, dict)
        patched_plan = copy.deepcopy(plan)
        sidecar = patched_plan.get("scope_signal_guard_decision_v2")
        if not isinstance(sidecar, dict):
            observed["original_plan"] = None
            return patched_plan, exit_code, stderr, stdout
        authority = sidecar.get("scope_delta_authority")
        if not isinstance(authority, dict):
            observed["original_plan"] = None
            return patched_plan, exit_code, stderr, stdout
        observed["original_plan"] = authority.get("contract_patch_plan")
        if replacement is _MISSING_PLANNER_PATCH_PLAN:
            authority.pop("contract_patch_plan", None)
        else:
            authority["contract_patch_plan"] = replacement
        return patched_plan, exit_code, stderr, stdout

    monkeypatch.setattr(_e2e_preflight, "_invoke_planner", invoke_without_patch_plan)
    return observed


@pytest.mark.parametrize(
    "replacement",
    [_MISSING_PLANNER_PATCH_PLAN, []],
    ids=["absent", "non_dict"],
)
def test_explicit_human_review_directive_reaches_next_action_production_reachable_when_planner_plan_missing(
    tmp_path, capsys, monkeypatch, replacement
):
    """AC1: only the canonical consumer routes eligible missing-plan input."""
    observed = _force_hrd_planner_patch_plan_missing(monkeypatch, replacement)

    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path, anchor_body=_HRD_EXPLICIT_BODY, run_id=f"missing_plan_{type(replacement).__name__}"
    )
    captured = capsys.readouterr()

    assert isinstance(observed["original_plan"], dict)
    assert "NEXT_ACTION: issue_editor_required" in captured.out
    assert result["next_action"] == "issue_editor_required"
    assert result["contract_update"]["reason_code"] == "explicit_trusted_human_directive_requires_issue_editor"
    assert result["contract_update"]["writes"] == 0
    assert calls["apply_transaction"] == 0
    assert calls["fetch_current"] >= 1


def _spy_classify_scope_delta_authority(monkeypatch):
    """PR #2732 OWNER REQUEST_CHANGES (P2): observe whether the freeform
    #2620 route's canonical, existing classifier
    (`scope_signal_delta.classify_scope_delta_authority()`, the SAME
    function `_decide_human_review_directive_editor_route()` calls for its
    own fresh re-classification) was invoked at all for a given
    negative-matrix case, and what `route` disposition it reached -- without
    reimplementing or duplicating that classifier's own logic. A standard
    module import is used (not the custom `importlib` reload
    `_e2e_load_module()` uses for `run_refinement_preflight.py`), so this is
    the SAME cached `scope_signal_delta` module object every local
    `from scope_signal_delta import classify_scope_delta_authority` call
    inside the production entrypoint resolves against.
    """
    import scope_signal_delta

    original = scope_signal_delta.classify_scope_delta_authority
    calls: list = []

    def spy(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(scope_signal_delta, "classify_scope_delta_authority", spy)
    return calls


def test_issue2715_origin_comment_reproduction_stays_fail_closed_at_expands_allowed_paths_boundary(
    tmp_path, monkeypatch
):
    """PR #2732 OWNER REQUEST_CHANGES (P1, iteration N): #2715's own origin
    comment (issuecomment-5770013860) carries an "Allowed Paths:" heading
    followed by exact repository-relative path bullets -- a structural
    shape `_HRD_EXPLICIT_BODY` (a short freeform onboarding sentence) never
    represented. This reproduces that exact structure
    (`_HRD_2715_ORIGIN_BODY`) and drives it through the REAL
    `run_preflight()` -> `consume_trusted_anchor_contract_patch_plan()` ->
    `_decide_human_review_directive_editor_route()` call chain, with
    `_force_hrd_planner_patch_plan_missing()` applied the same way the
    existing #2724 positive fixture uses it.

    OBSERVED (not assumed) outcome, confirmed by running this fixture
    before writing any assertion: this body's own "Allowed Paths:" heading
    trips `scope_signal_delta.detect_boundary_flags()`'s
    `expands_allowed_paths` boundary (the heading text alone matches
    `_ALLOWED_PATHS_DIRECTIVE_RE`), while none of the individual bullet
    items below it independently carries the phrase "allowed paths"
    together with a path literal in the SAME extracted directive item
    (`_has_explicit_exact_allowed_path_expansion()` requires both in one
    item), and no `investigation_derived_path_literals` were supplied. The
    FRESH `classify_scope_delta_authority()` re-classification therefore
    resolves `route.action == "human_escalation"` /
    `route.reason_code == "expands_allowed_paths"` -- never
    `contract_update_required` -- so
    `decide_human_review_directive_editor_route()` (the #2620 SSOT) is
    never eligible, `_decide_human_review_directive_editor_route()` returns
    `None`, and the transaction falls through to the PRE-EXISTING
    `contract_patch_plan_missing` fail-closed path. This is NOT the #2620
    `issue_editor_required` handoff. It is a genuine, independently
    reproduced fail-closed outcome for this exact real-world comment shape,
    pinned here as its own regression so it can never silently regress into
    either a false `issue_editor_required` promotion or a silent
    operations[] synthesis. Per #2724's Stop Conditions, this residual
    boundary is NOT relaxed here (that would be a classifier-eligibility
    change, out of this Issue's Allowed Paths/In Scope) -- it is only
    observed and fixed as a regression.
    """
    observed = _force_hrd_planner_patch_plan_missing(monkeypatch)
    classify_calls = _spy_classify_scope_delta_authority(monkeypatch)

    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path, anchor_body=_HRD_2715_ORIGIN_BODY, run_id="issue2715_origin_reproduction"
    )

    # The planner's OWN (pre-force) authority classification never carried a
    # contract_patch_plan for this body either -- the expands_allowed_paths
    # boundary already blocks it before this helper's forced removal has
    # anything to remove. This is a materially different missing-plan cause
    # than `_HRD_EXPLICIT_BODY`'s genuine planner-omission case above, and is
    # asserted explicitly rather than assumed.
    assert observed["original_plan"] is None

    assert result["next_action"] != "issue_editor_required"
    assert result["next_action"] == "human_judgment_required"
    assert "CONTRACT_UPDATE_FAILED" in result["blockers"]
    assert "PLANNER_FAIL_CLOSED" not in result["blockers"]
    assert result["planner_fail_closed"] is False
    assert result["planner_fail_closed_reason_codes"] == []
    assert result["contract_update"]["status"] == "failed"
    assert result["contract_update"]["writes"] == 0
    assert result["contract_update"]["reason_code"] == "contract_patch_plan_missing"
    assert calls["apply_transaction"] == 0
    # The boundary check short-circuits before the #2620 route's own fresh,
    # TOCTOU-safe fetch_current() readback is ever reached.
    assert calls["fetch_current"] == 0

    # Independently pin WHY this stays fail-closed: the SAME classifier the
    # production route calls resolves human_escalation /
    # expands_allowed_paths for this exact evidence, not some other
    # unrelated fail-closed cause.
    assert len(classify_calls) == 1
    assert classify_calls[0]["route"]["action"] == "human_escalation"
    assert classify_calls[0]["route"]["reason_code"] == "expands_allowed_paths"


@pytest.mark.parametrize(
    (
        "name",
        "anchor_body",
        "author_association",
        "human_context_urls",
        "context_extra",
        "replacement",
        "expect_classifier_reached",
        "expected_authority_reason_code",
    ),
    [
        (
            "wrong_lane",
            _HRD_EXPLICIT_BODY,
            "OWNER",
            (),
            None,
            _MISSING_PLANNER_PATCH_PLAN,
            True,
            "ai_inferred_scope_delta",
        ),
        (
            "untrusted",
            _HRD_EXPLICIT_BODY,
            "NONE",
            (_HRD_URL,),
            None,
            _MISSING_PLANNER_PATCH_PLAN,
            True,
            "untrusted_author_association",
        ),
        (
            "ambiguous",
            _HRD_PROSE_ONLY_BODY,
            "OWNER",
            (_HRD_URL,),
            None,
            _MISSING_PLANNER_PATCH_PLAN,
            True,
            "ambiguous_human_directive",
        ),
        (
            "invalid_binding",
            _HRD_INVALID_BINDING_BODY,
            "OWNER",
            (_HRD_URL,),
            None,
            _MISSING_PLANNER_PATCH_PLAN,
            False,
            None,
        ),
        # PR #2732 OWNER REQUEST_CHANGES (P2 item 4): a representative
        # non-dict (malformed) planner `contract_patch_plan` case, reusing
        # the existing `replacement=[]` pattern already exercised by
        # `test_explicit_human_review_directive_reaches_next_action_
        # production_reachable_when_planner_plan_missing` -- combined here
        # with the `untrusted` ineligibility cause to confirm a malformed
        # non-dict artifact is never coerced into a different (more
        # permissive) outcome than the equivalent "absent" case.
        (
            "untrusted_non_dict_plan",
            _HRD_EXPLICIT_BODY,
            "NONE",
            (_HRD_URL,),
            None,
            [],
            True,
            "untrusted_author_association",
        ),
    ],
)
def test_missing_planner_plan_ineligible_input_remains_fail_closed_production_reachable(
    tmp_path,
    monkeypatch,
    name,
    anchor_body,
    author_association,
    human_context_urls,
    context_extra,
    replacement,
    expect_classifier_reached,
    expected_authority_reason_code,
):
    """AC3: missing planner input never becomes a genuine empty-plan noop.

    PR #2732 OWNER REQUEST_CHANGES (P2): each case additionally pins (1)
    whether the #2620 freeform route's canonical classifier
    (`scope_signal_delta.classify_scope_delta_authority()`) was reached at
    all for this ineligibility cause, and (2) when reached, the exact
    `route.reason_code` it independently resolved -- distinguishing the
    shared `CONTRACT_UPDATE_FAILED` marker's DIFFERENT underlying causes
    (wrong lane / untrusted author / ambiguous directive / never reached at
    all for a binding-mismatched structured payload) instead of treating
    them as one interchangeable outcome.
    """
    _force_hrd_planner_patch_plan_missing(monkeypatch, replacement)
    classify_calls = _spy_classify_scope_delta_authority(monkeypatch)

    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path,
        anchor_body=anchor_body,
        run_id=f"missing_plan_{name}",
        author_association=author_association,
        human_context_comment_urls=human_context_urls,
        known_context_extra=context_extra,
    )

    assert result["next_action"] != "issue_editor_required"
    assert result["contract_update"]["writes"] == 0
    assert result["contract_update"]["status"] == "failed"
    assert result["contract_update"]["disposition"] not in {"proven_no_change", "no_change"}
    assert calls["apply_transaction"] == 0
    assert "CONTRACT_UPDATE_FAILED" in result["blockers"]
    assert "PLANNER_FAIL_CLOSED" not in result["blockers"]
    assert result["planner_fail_closed"] is False
    assert result["planner_fail_closed_reason_codes"] == []
    assert result["contract_update"]["reason_code"] == "contract_patch_plan_missing"

    if expect_classifier_reached:
        assert len(classify_calls) == 1
        assert classify_calls[0]["route"]["action"] == "human_escalation"
        assert classify_calls[0]["route"]["reason_code"] == expected_authority_reason_code
    else:
        assert classify_calls == []


def test_ac3_untrusted_author_association_never_escalates_production_reachable(tmp_path):
    """AC3: an untrusted author association (NONE) never reaches
    issue_editor_required through the real run_preflight() -- fails
    closed, writes=0."""
    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path, anchor_body=_HRD_EXPLICIT_BODY, run_id="untrusted", author_association="NONE"
    )

    assert result["next_action"] != "issue_editor_required"
    assert calls["apply_transaction"] == 0


def test_ac4_ambiguous_prose_only_directive_never_escalates_production_reachable(tmp_path):
    """AC4: a freeform human-context comment with no bullet-list directive
    content (no imperative ask) never reaches explicit confidence -- fails
    closed through the real run_preflight(), never issue_editor_required."""
    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path, anchor_body=_HRD_PROSE_ONLY_BODY, run_id="ambiguous"
    )

    assert result["next_action"] != "issue_editor_required"
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# PR #2623 review fix (P1 finding 1): `investigation_derived_path_literals`
# forwarding from `known_context` into the fresh
# `classify_scope_delta_authority()` re-classification inside
# `_decide_human_review_directive_editor_route()`. Prior to this fix, a
# trusted operator-selected human-context directive that named an
# architecture/workflow-level Allowed Paths expansion in prose (no exact
# backtick literal in the comment itself) stayed fail-closed at
# `human_escalation` (`expands_allowed_paths` boundary) EVEN WHEN the caller
# had already supplied agent-investigation-derived exact path literals on
# `known_context` -- because this specific call site silently dropped that
# key. Both tests below exercise the REAL run_preflight() -> consume_
# trusted_anchor_contract_patch_plan() -> _decide_human_review_directive_
# editor_route() call chain (production-reachable, not a unit-level
# shortcut).
# ---------------------------------------------------------------------------

_HRD_VAGUE_ALLOWED_PATHS_BODY = "\n".join(
    [
        "この issue-refinement-loop の欠陥は他の workflow skill にも共通するため、",
        "allowed paths を必要に応じて拡張してください。",
        "- impl-review-loop も合わせて直してください。",
        "- build_intake_capsule と implement-issue の関連処理も修正してください。",
    ]
)
_HRD_INVESTIGATION_DERIVED_LITERALS = [
    "docs/dev/workflow.md",
    ".claude/skills/impl-review-loop/SKILL.md",
    ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
    ".claude/skills/implement-issue/SKILL.md",
]


def test_finding1_investigation_derived_path_literals_forwarded_reaches_issue_editor_required(tmp_path):
    """A trusted, explicit, with_human_context directive that only names an
    Allowed Paths expansion in semantic prose (no exact backtick literal)
    would normally fail closed at `expands_allowed_paths` boundary
    (`human_escalation`, #1952). When the caller has already derived exact
    repository-relative path literals via read-only investigation
    (`known_context["investigation_derived_path_literals"]`, #2086 AC3/AC4)
    and `contract_patch_plan.operations` still resolves empty (no known
    section marker heading in the comment, so no section-bound patch can be
    built either), this must reach `NEXT_ACTION: issue_editor_required`
    with `writes == 0` -- the exact #2620 handoff, now reachable for this
    lane."""
    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path,
        anchor_body=_HRD_VAGUE_ALLOWED_PATHS_BODY,
        run_id="investigation_literals_forwarded",
        known_context_extra={"investigation_derived_path_literals": _HRD_INVESTIGATION_DERIVED_LITERALS},
    )

    assert result["next_action"] == "issue_editor_required"
    assert result["contract_update"]["writes"] == 0
    assert calls["apply_transaction"] == 0
    assert calls["fetch_current"] >= 1


def test_finding1_regression_without_investigation_derived_path_literals_stays_fail_closed(tmp_path):
    """The SAME vague Allowed-Paths-expansion directive, but WITHOUT
    `investigation_derived_path_literals` on `known_context` -- the
    pre-existing #1952 fail-closed lock is unaffected by this fix, it is
    only ever cleared when the caller explicitly supplies validated
    investigation-derived literals."""
    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path,
        anchor_body=_HRD_VAGUE_ALLOWED_PATHS_BODY,
        run_id="investigation_literals_absent",
    )

    assert result["next_action"] != "issue_editor_required"
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# PR #2623 review fix (P1 finding 2): TOCTOU-safe fresh readback inside
# `_decide_human_review_directive_editor_route()`. A concurrent anchor edit
# between evidence capture and this routing decision must never authorize a
# handoff on stale evidence -- production-reachable via the REAL
# run_preflight() call chain (see `test_ac1_ac6_explicit_human_review_
# directive_reaches_next_action_production_reachable` above for the
# unchanged-anchor positive case, which already asserts
# `calls["fetch_current"] >= 1`).
# ---------------------------------------------------------------------------


def test_finding2_anchor_drift_at_fresh_readback_fails_closed_production_reachable(tmp_path):
    """The SAME explicit, eligible directive as the AC1/AC6 positive case,
    but the anchor comment's body has changed by the time the fresh
    `fetch_current()` readback runs (simulating a concurrent edit) -- this
    must NOT reach `issue_editor_required`; it fails closed to the existing
    fallback (`no_change`) instead, with writes == 0."""
    result, _exit_code, calls = _hrd_run_preflight(
        tmp_path,
        anchor_body=_HRD_EXPLICIT_BODY,
        run_id="anchor_drift",
        fresh_anchor_body=_HRD_EXPLICIT_BODY + "\nEdited after evidence capture.\n",
    )

    assert result["next_action"] != "issue_editor_required"
    assert result["contract_update"]["writes"] == 0
    assert calls["apply_transaction"] == 0
    # The fresh readback genuinely ran and detected the drift -- this is
    # not merely "never checked", it is "checked and correctly rejected".
    assert calls["fetch_current"] >= 1
