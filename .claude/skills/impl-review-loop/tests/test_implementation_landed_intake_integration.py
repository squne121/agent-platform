"""GIVEN/WHEN/THEN integration tests for the #2699 pre-Step-1 choke point."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
ROUTE = ROOT / ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
RUNTIME = ROOT / ".claude/skills/impl-review-loop/scripts/verify_implementation_landed_intake_runtime.py"

# research Issue #2761: mirrors `implementation_landed_evidence.py`'s
# `_LIVE_CANDIDATE_REFRESH_FIELDS` -- the decision-time per-candidate
# `gh pr view` field list used by `_live_freshness_reference()`, now
# including `body,closingIssuesReferences` so qualification can be freshly
# re-derived at decision time (AC1).
_LIVE_CANDIDATE_REFRESH_FIELDS = "headRefOid,mergedAt,mergeCommit,body,closingIssuesReferences"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _evidence():
    return {
        "schema": "IMPLEMENTATION_LANDED_EVIDENCE_V1",
        "schema_version": 1,
        "target": {
            "repo": "squne121/agent-platform",
            "issue_number": 2119,
            "body_sha256": "sha256:" + "b" * 64,
        },
        "freshness": {"status": "fresh", "observed_at": "2026-09-21T00:00:00Z", "max_age_seconds": 300},
        "contradictory": False,
        "candidates": [
            {
                "target": {"repo": "squne121/agent-platform", "issue_number": 2119},
                "pr": {"number": 2137},
                "provenance": {"kind": "closing_relation", "verified": True},
                "lifecycle": "merged",
                "merge_oid": "a" * 40,
                "main_ancestry": {"verified": True, "reachable": True},
                "head_fresh": False,
                "current_scope_ownership": False,
            }
        ],
        "scope_coverage": {
            "schema": "IMPLEMENTATION_SCOPE_COVERAGE_V1",
            "immutable_merged_snapshot": {"content_sha256": "sha256:" + "c" * 64},
            "live_current_scope": {"content_sha256": "sha256:" + "c" * 64},
            "exact_coverage": True,
            "later_scope_expansion": False,
        },
    }


def test_disposition_precedes_worker_worktree_and_new_pr():
    """GIVEN landed or unsafe evidence WHEN pre-Step-1 routes THEN data-plane start is forbidden.

    #2713 AC1/AC5: `resolve_pre_step1_data_plane_action()` now takes an
    already-resolved disposition mapping directly (no raw-evidence
    recompute inside it) -- the caller derives the disposition via
    `resolve_pre_step1_landing_disposition()` first, exactly once."""
    route = _load(ROUTE, "route_loop_verdict_v2_landing")
    landed = route.resolve_pre_step1_landing_disposition(_evidence(), repo="squne121/agent-platform", issue_number=2119)
    unsafe = route.resolve_pre_step1_landing_disposition(None, repo="squne121/agent-platform", issue_number=2119)
    landed_gate = route.resolve_pre_step1_data_plane_action(landed)
    unsafe_gate = route.resolve_pre_step1_data_plane_action(unsafe)
    assert landed["disposition"] == "implementation_already_landed"
    assert unsafe["disposition"] == "reconciliation_required"
    assert landed_gate == {
        "disposition": landed,
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }
    assert unsafe_gate["start_data_plane"] is False
    preparation = (ROOT / ".claude/skills/impl-review-loop/steps/preparation.md").read_text(encoding="utf-8")
    assert preparation.index("Evidence-Based Landing Disposition") < preparation.index("Already-Satisfied Early-Exit")
    assert "worker / worktree / new PR を開始せず" in preparation


BUILD_CAPSULE = ROOT / ".claude/skills/impl-review-loop/scripts/build_intake_capsule.py"
LANDED_EVIDENCE = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"


def _candidate(*, lifecycle: str, number: int = 2137) -> dict:
    candidate: dict = {
        "target": {"repo": "squne121/agent-platform", "issue_number": 2119},
        "pr": {"number": number, "url": f"https://github.com/squne121/agent-platform/pull/{number}"},
        "provenance": {"kind": "closing_relation", "verified": True},
        "lifecycle": lifecycle,
        "head_fresh": True,
        "current_scope_ownership": False,
    }
    if lifecycle == "merged":
        candidate.update({"merge_oid": "a" * 40, "main_ancestry": {"verified": True, "reachable": True}})
    return candidate


def test_pr_exists_is_generated_from_candidate_lifecycle_pr_fixtures_not_injected_booleans():
    """#2713 AC3: `pr_exists` is generated from PR fixture lifecycle shapes
    (open / draft / closed-unmerged / merged / no-qualified-candidate) via
    the production `derive_pr_exists_from_landing_candidate()` function --
    tests never inject the `pr_exists` boolean into `_collect_implementation
    _landed_evidence()` directly."""
    landed_evidence = _load(LANDED_EVIDENCE, "implementation_landed_evidence_for_pr_fixture_test")
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="open")) is True
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="draft")) is True
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="merged")) is True
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="closed_unmerged")) is False
    assert landed_evidence.derive_pr_exists_from_landing_candidate(None) is False


def test_collect_implementation_landed_evidence_applies_precedence_through_default_loader(monkeypatch):
    """#2713 AC3/AC4/AC5: GIVEN a no-landing-authority evidence result
    (closed-unmerged candidate) and a fresh independent base_ac_verification
    _result WHEN `build_intake_capsule.py::_collect_implementation_landed_
    evidence()` (the production entry point) runs WITHOUT any
    `route_loop_verdict_v2_module` injection (default loader only) THEN
    `apply_already_satisfied_precedence()` actually fires (proving the
    #2713 AC4 `sys.modules` registration fix works end-to-end through the
    production entry point, not merely a dependency-injected unit test) and
    the pre_step1_data_plane key set (`start_data_plane` / `action`) stays
    unchanged (AC6)."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_landed_intake_test")

    main_sha = "9" * 40
    pr_view_full = json.dumps(
        {
            "number": 2137,
            "url": "https://github.com/squne121/agent-platform/pull/2137",
            "state": "CLOSED",
            "isDraft": False,
            "mergedAt": None,
            "mergeCommit": None,
            "headRefOid": "c" * 40,
            "closingIssuesReferences": [{"number": 2119}],
            "body": "",
            "files": [],
        }
    )
    pr_list = json.dumps([{"number": 2137, "closingIssuesReferences": [{"number": 2119}]}])

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, pr_list, ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        if argv[:3] == ["gh", "pr", "view"]:
            if argv[-1] == _LIVE_CANDIDATE_REFRESH_FIELDS:
                return (
                    0,
                    json.dumps(
                        {
                            "headRefOid": "c" * 40,
                            "mergedAt": None,
                            "mergeCommit": None,
                            "body": "",
                            "closingIssuesReferences": [{"number": 2119}],
                        }
                    ),
                    "",
                )
            return 0, pr_view_full, ""
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"body": "live body"}), ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    monkeypatch.setattr(build_capsule, "_run_command", run)

    evidence = build_capsule._collect_implementation_landed_evidence(
        issue_number=2119,
        repo="squne121/agent-platform",
        issue_body="live body",
        command_log=[],
        next_action_route="proceed_to_step_1",
        # #2713 AC9 (PR #2741 review, P1-2): a fully-shaped
        # TEST_VERDICT_MACHINE/v2 payload -- `derive_base_ac_satisfied_from_
        # verification_result()` now reuses `adjudicate_vc_result.py::
        # adapt_test_verdict_to_current_vc_result()`'s validation, which
        # requires `schema`, `contract_body_sha256`, and per-entry
        # `command_hash` -- a bare `{"head_sha", "runtime_ac_results": [{"ac",
        # "status"}]}` (the previous, weaker fixture shape this test used) is
        # exactly the kind of incomplete payload AC9 requires NOT to be
        # trusted as True.
        base_ac_verification_result={
            "schema": "TEST_VERDICT_MACHINE/v2",
            "generated_at": "2026-09-24T00:00:00Z",
            "head_sha": main_sha,
            "contract_body_sha256": "sha256:" + "d" * 64,
            "result": "PASS",
            "runtime_ac_results": [
                {
                    "ac": "AC1",
                    "command_hash": "sha256:" + "1" * 64,
                    "status": "pass",
                    "exit_code": 0,
                    "fallback_detected": False,
                    "human_review_required": False,
                    "stop_condition_triggered": False,
                }
            ],
        },
    )

    assert evidence["landing_disposition"]["disposition"] == "already_satisfied"
    assert set(evidence["pre_step1_data_plane"]) == {"start_data_plane", "action"}
    assert evidence["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }


def test_collect_implementation_landed_evidence_skips_precedence_when_base_ac_undeterminable(monkeypatch):
    """#2713 In Scope: GIVEN no `base_ac_verification_result` (today's
    `build_intake_capsule.py` CLI has no production source for it) WHEN
    `_collect_implementation_landed_evidence()` runs THEN
    `apply_already_satisfied_precedence()` is skipped (never a fabricated
    True/False fallback) and the freshness-rebound landing disposition
    passes through unchanged."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_landed_intake_skip_test")

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, "9" * 40 + "\n", ""
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"body": "live body"}), ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    monkeypatch.setattr(build_capsule, "_run_command", run)

    evidence = build_capsule._collect_implementation_landed_evidence(
        issue_number=2119,
        repo="squne121/agent-platform",
        issue_body="live body",
        command_log=[],
        next_action_route="proceed_to_step_1",
    )

    assert evidence["landing_disposition"]["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert evidence["landing_disposition"]["reason_codes"] == ["no_qualified_candidate"]
    assert evidence["pre_step1_data_plane"] == {"start_data_plane": True, "action": "dispatch_step1"}


_NO_CANDIDATE_ISSUE_BODY = "## Machine-Readable Contract\n\nstatus: full-body\n"


def _no_candidate_already_satisfied_run(issue_body: str, main_sha: str):
    """#2713 AC8 (PR #2741 review, P1-1): a full `_run_command` fixture
    covering `build_intake_capsule()`'s ENTIRE production call graph
    (issue metadata, repo state, comments, and the
    `_collect_implementation_landed_evidence()` candidate discovery +
    freshness rebind) for a no-qualified-PR-candidate scenario, driven by
    argv shape (not call-order position) so it is robust across the
    multiple entry points (`build_intake_capsule()`, `main()`) these tests
    exercise."""

    def run(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return 0, "f" * 40 + "\n", ""
        if argv[:2] == ["git", "branch"]:
            return 0, "main\n", ""
        if argv[:2] == ["git", "status"]:
            return 0, "", ""
        if argv[:3] == ["gh", "issue", "view"]:
            json_flag_index = argv.index("--json")
            if argv[json_flag_index + 1] == "body":
                return 0, json.dumps({"body": issue_body}), ""
            return (
                0,
                json.dumps(
                    {
                        "title": "実装: production reachability テスト",
                        "state": "open",
                        "labels": [{"name": "phase/implementation"}],
                        "body": issue_body,
                        "updatedAt": "2026-09-24T00:00:00Z",
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 3 and "comments?per_page" in argv[3]:
            return 0, "", ""
        if argv[:2] == ["gh", "api"] and "timeline" in argv[-1]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    return run


def _fresh_test_verdict(main_sha: str) -> dict:
    return {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "generated_at": "2026-09-24T00:00:00Z",
        "head_sha": main_sha,
        "contract_body_sha256": "sha256:" + "e" * 64,
        "result": "PASS",
        "runtime_ac_results": [
            {
                "ac": "AC1",
                "command_hash": "sha256:" + "1" * 64,
                "status": "pass",
                "exit_code": 0,
                "fallback_detected": False,
                "human_review_required": False,
                "stop_condition_triggered": False,
            }
        ],
    }


def test_public_build_intake_capsule_reaches_already_satisfied_via_base_ac_verification_result(monkeypatch):
    """#2713 AC8 (PR #2741 review, P1-1): GIVEN a `base_ac_verification_result`
    supplied through the PUBLIC `build_intake_capsule()` function (the
    canonical production entry point -- not `_collect_implementation_landed_
    evidence()` called directly) WHEN no qualified PR candidate exists and
    the supplied verification result is fresh and fully clean THEN the
    capsule's `implementation_landed_evidence.landing_disposition.
    disposition` reaches `already_satisfied` and
    `pre_step1_data_plane.start_data_plane` reaches `False` -- proving
    production reachability through the public API boundary, not merely a
    private-helper direct-argument-injection unit test."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_public_reachability_test")
    main_sha = "9" * 40

    monkeypatch.setattr(
        build_capsule,
        "_run_command",
        _no_candidate_already_satisfied_run(_NO_CANDIDATE_ISSUE_BODY, main_sha),
    )

    capsule, _artifact, exit_code = build_capsule.build_intake_capsule(
        2119,
        "squne121/agent-platform",
        None,
        include_implementation_landed_evidence=True,
        base_ac_verification_result=_fresh_test_verdict(main_sha),
    )

    assert exit_code == 0
    landed = capsule["implementation_landed_evidence"]
    assert landed["landing_disposition"]["disposition"] == "already_satisfied"
    assert landed["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }


def test_cli_main_reaches_already_satisfied_via_base_ac_verification_result_file(tmp_path, monkeypatch):
    """#2713 AC8 (PR #2741 review, P1-1): the `main()` canonical CLI
    entrypoint (argparse -> build_intake_capsule -> artifact write), driven
    by `--base-ac-verification-result-file`, reaches the SAME
    already_satisfied / start_data_plane:false outcome as the public-function
    test above -- proving the flag is wired end-to-end from the actual CLI
    boundary (not merely the Python function signature)."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_cli_reachability_test")
    main_sha = "9" * 40

    monkeypatch.setattr(
        build_capsule,
        "_run_command",
        _no_candidate_already_satisfied_run(_NO_CANDIDATE_ISSUE_BODY, main_sha),
    )

    verification_result_file = tmp_path / "base-ac-verification-result.json"
    verification_result_file.write_text(json.dumps(_fresh_test_verdict(main_sha)), encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"

    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        "2119",
        "--repo",
        "squne121/agent-platform",
        "--max-stdout-bytes",
        "65536",
        "--include-implementation-landed-evidence",
        "--base-ac-verification-result-file",
        str(verification_result_file),
        "--artifact-dir",
        str(artifact_dir),
    ]

    with patch.object(sys, "argv", argv):
        exit_code = build_capsule.main()

    assert exit_code == 0
    artifact_payload = json.loads((artifact_dir / "intake-capsule-2119.json").read_text(encoding="utf-8"))
    landed = artifact_payload["implementation_landed_evidence"]
    assert landed["landing_disposition"]["disposition"] == "already_satisfied"
    assert landed["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }


def test_cli_main_base_ac_verification_result_file_missing_is_fail_safe_not_fatal(tmp_path, monkeypatch):
    """#2713 In Scope: a missing/unreadable
    `--base-ac-verification-result-file` must degrade to `None`
    (undeterminable) rather than blocking intake (non-zero exit) or being
    fabricated into a True/False `base_ac_satisfied` -- the existing
    freshness-rebound landing disposition passes through unchanged."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_cli_missing_file_test")
    main_sha = "9" * 40

    monkeypatch.setattr(
        build_capsule,
        "_run_command",
        _no_candidate_already_satisfied_run(_NO_CANDIDATE_ISSUE_BODY, main_sha),
    )

    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        "2119",
        "--repo",
        "squne121/agent-platform",
        "--max-stdout-bytes",
        "65536",
        "--include-implementation-landed-evidence",
        "--base-ac-verification-result-file",
        str(tmp_path / "does-not-exist.json"),
        "--artifact-dir",
        str(artifact_dir),
    ]

    with patch.object(sys, "argv", argv):
        exit_code = build_capsule.main()

    assert exit_code == 0
    artifact_payload = json.loads((artifact_dir / "intake-capsule-2119.json").read_text(encoding="utf-8"))
    landed = artifact_payload["implementation_landed_evidence"]
    assert landed["landing_disposition"]["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert landed["pre_step1_data_plane"] == {"start_data_plane": True, "action": "dispatch_step1"}


def test_live_runtime_verifier_records_2119_2137_legacy_compatibility_without_fixture_fallback(tmp_path):
    """AC12: GIVEN live-shaped #2119/#2137 read responses (a legacy merged
    candidate with no durable IMPLEMENTATION_SCOPE_COVERAGE_V1 marker) WHEN
    the runtime verifier runs THEN it records non-fallback evidence and the
    legacy-compatibility disposition itself is derived through the same
    production `derive_landing_disposition()` function `build_intake_capsule
    .py` uses -- not a verifier-local inline ternary -- and correctly stays
    ordinary_dispatch_or_explicit_recovery (never implementation_already_landed)."""
    runtime = _load(RUNTIME, "implementation_landed_runtime")
    issue = {"number": 2119, "url": "https://github.com/squne121/agent-platform/issues/2119", "body": "live body"}
    pr = {
        "number": 2137,
        "url": "https://github.com/squne121/agent-platform/pull/2137",
        "state": "MERGED",
        "mergeCommit": {"oid": "a" * 40},
        "closingIssuesReferences": [],
    }
    responses = [(0, json.dumps(issue), ""), (0, json.dumps(pr), ""), (0, "ahead\n", "")]

    def run(_argv):
        return responses.pop(0)

    artifact = tmp_path / "artifacts" / "runtime-verification-AC12-test.log"
    payload, code = runtime.verify(artifact_path=artifact, run_command=run)
    assert code == 0
    assert payload["status"] == "PASS"
    assert payload["fallback_used"] is False
    assert payload["legacy_scope_coverage_marker_missing"] is True
    assert payload["legacy_compatibility_disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert artifact.exists()


def test_collect_candidate_inputs_production_shaped_2727_sibling_cross_references_excluded(monkeypatch):
    """AC9: production-shaped regression through `collect_candidate_inputs()`
    (timeline candidate collection -> real `gh pr view` PR body fetch -> a
    genuine `IMPLEMENTATION_SCOPE_COVERAGE_V1` marker text produced by
    `build_scope_coverage_marker()`/`render_scope_coverage_marker()` and
    parsed by `coverage_from_pr_body()` -> candidate qualification via
    `derive_landing_disposition()`), reusing this module's existing mock
    `gh` pattern -- not a hand-built candidate dict. Reproduces the exact
    #2727 incident shape: PR #2735 and PR #2746 analogues are discovered as
    timeline cross-references for the current target Issue #2727, but each
    carries a well-formed durable marker naming a *different* sibling Issue
    (#2725 / #2726). Neither is a qualified landing candidate for the
    current target, and `qualified_candidate_conflict` never fires (AC1/AC2).

    #2750 PR #2758 review (P1 blocker, comment
    legacy-repository PR #2758 issuecomment-5831103538):
    this test used to stop at `derive_landing_disposition()`. It now
    continues the SAME #2727 incident-shaped fixture through the canonical
    production composition `build_intake_capsule.py::
    _collect_implementation_landed_evidence()` actually runs
    (`resolve_landing_disposition_with_freshness_rebind()` ->
    `derive_landing_disposition()` -> `apply_already_satisfied_precedence()`
    -> `resolve_pre_step1_data_plane_action()`), reusing the existing mock
    `gh` / `base_ac_verification_result` fixture patterns from
    `test_collect_implementation_landed_evidence_applies_precedence_through_
    default_loader()` above -- never a bespoke test-only re-implementation
    of that composition logic."""
    landed_evidence = _load(LANDED_EVIDENCE, "implementation_landed_evidence_for_2727_integration_test")

    target_issue = 2727
    repo = "squne121/agent-platform"
    target_issue_body = "## Allowed Paths\n- `.claude/a.py`\n"
    sibling_body_2725 = "## Allowed Paths\n- `.claude/x.py`\n"
    sibling_body_2726 = "## Allowed Paths\n- `.claude/y.py`\n"
    main_sha = "9" * 40

    marker_2735 = landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(
            issue_number=2725, issue_body=sibling_body_2725, pr_head_sha="a" * 40
        )
    )
    marker_2746 = landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(
            issue_number=2726, issue_body=sibling_body_2726, pr_head_sha="b" * 40
        )
    )

    timeline_events = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 2735,
                    "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/2735"},
                    "repository_url": f"https://api.github.com/repos/{repo}",
                }
            },
        },
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 2746,
                    "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/2746"},
                    "repository_url": f"https://api.github.com/repos/{repo}",
                }
            },
        },
    ]

    # #2750 PR #2758 review / research Issue #2761: `_LIGHT_PR_FIELDS`
    # distinguishes the freshness-rebind `gh pr view --json
    # headRefOid,mergedAt,mergeCommit,body,closingIssuesReferences` shape
    # (`_live_freshness_reference()`) from the discovery-time
    # `gh pr view --json number,url,state,...` shape (`collect_candidate_
    # inputs()`) -- both real production call shapes the same fixture must
    # answer once this test continues through
    # `resolve_landing_disposition_with_freshness_rebind()`. #2761: the
    # light-field response now also carries `body`/`closingIssuesReferences`
    # so decision-time re-qualification independently reconfirms each
    # sibling is still irrelevant from FRESH live data (never merely trusted
    # from collection time).
    _LIGHT_PR_FIELDS = _LIVE_CANDIDATE_REFRESH_FIELDS

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            # Neither PR's closingIssuesReferences names the current target
            # Issue #2727 -- both close a different sibling Issue instead.
            return (
                0,
                json.dumps(
                    [
                        {"number": 2735, "closingIssuesReferences": [{"number": 2725}]},
                        {"number": 2746, "closingIssuesReferences": [{"number": 2726}]},
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, json.dumps(timeline_events), ""
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2735" and argv[-1] == _LIGHT_PR_FIELDS:
            return (
                0,
                json.dumps(
                    {
                        "headRefOid": "a" * 40,
                        "mergedAt": "2026-09-01T00:00:00Z",
                        "mergeCommit": {"oid": "a" * 40},
                        "body": marker_2735,
                        "closingIssuesReferences": [{"number": 2725}],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2746" and argv[-1] == _LIGHT_PR_FIELDS:
            return (
                0,
                json.dumps(
                    {
                        "headRefOid": "b" * 40,
                        "mergedAt": "2026-09-02T00:00:00Z",
                        "mergeCommit": {"oid": "b" * 40},
                        "body": marker_2746,
                        "closingIssuesReferences": [{"number": 2726}],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2735":
            return (
                0,
                json.dumps(
                    {
                        "number": 2735,
                        "url": f"https://github.com/{repo}/pull/2735",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-09-01T00:00:00Z",
                        "mergeCommit": {"oid": "a" * 40},
                        "headRefOid": "a" * 40,
                        "closingIssuesReferences": [{"number": 2725}],
                        "body": marker_2735,
                        "files": [],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2746":
            return (
                0,
                json.dumps(
                    {
                        "number": 2746,
                        "url": f"https://github.com/{repo}/pull/2746",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-09-02T00:00:00Z",
                        "mergeCommit": {"oid": "b" * 40},
                        "headRefOid": "b" * 40,
                        "closingIssuesReferences": [{"number": 2726}],
                        "body": marker_2746,
                        "files": [],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"] and argv[3] == str(target_issue):
            return 0, json.dumps({"body": target_issue_body}), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "compare" in argv[2]:
            # research Issue #2761: neither sibling's own main-ancestry
            # compare may be invoked at all once qualified irrelevant
            # (skip-by-construction) -- this branch existing/succeeding is
            # not itself proof of correctness; `ancestry_compare_calls`
            # below asserts it is never actually reached.
            ancestry_compare_calls.append(argv)
            return 0, "ahead\n", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    ancestry_compare_calls: list[list[str]] = []
    evidence = landed_evidence.collect_candidate_inputs(
        repo=repo, issue_number=target_issue, current_scope=target_issue_body, run_command=run
    )
    assert ancestry_compare_calls == []
    assert len(evidence["candidates"]) == 2
    for candidate in evidence["candidates"]:
        assert candidate["provenance"]["kind"] == "verified_cross_reference"
        assert candidate["scope_coverage"]["status"] == "invalid"
        assert candidate["scope_coverage"]["errors"] == ["scope_coverage_issue_identity_mismatch"]
        assert candidate["qualified_irrelevant_sibling"] is True
        assert candidate["main_ancestry"] == {"verified": False, "reachable": False}

    result = landed_evidence.derive_landing_disposition(evidence, repo=repo, issue_number=target_issue)
    assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert result["reason_codes"] == ["no_qualified_candidate"]

    # #2750 PR #2758 review (P1 blocker): continue the SAME #2727
    # incident-shaped fixture through the canonical production entry point
    # `build_intake_capsule.py::_collect_implementation_landed_evidence()`
    # -- `resolve_landing_disposition_with_freshness_rebind()` ->
    # `derive_landing_disposition()` -> `apply_already_satisfied_precedence()`
    # -> `resolve_pre_step1_data_plane_action()` -- with a fresh, clean
    # base AC PASS `base_ac_verification_result` (reusing `_fresh_test_
    # verdict()` from above) so the no-qualified-candidate disposition
    # composes into `already_satisfied` / `suppress_worker_worktree_new_pr`,
    # never re-deriving that composition locally in the test.
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_2727_integration_test")
    monkeypatch.setattr(build_capsule, "_run_command", run)

    production_evidence = build_capsule._collect_implementation_landed_evidence(
        issue_number=target_issue,
        repo=repo,
        issue_body=target_issue_body,
        command_log=[],
        next_action_route="proceed_to_step_1",
        base_ac_verification_result=_fresh_test_verdict(main_sha),
    )

    assert production_evidence["landing_disposition"]["reason_codes"] != ["qualified_candidate_conflict"]
    assert production_evidence["landing_disposition"]["disposition"] == "already_satisfied"
    assert production_evidence["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }


# ---------------------------------------------------------------------------
# #2972: markerless open/draft PR whose populated `files` cover two exact
# entries and a tests/** entry. The fake transport responds to the real
# collection AND freshness-rebind command shapes.
# ---------------------------------------------------------------------------

_MARKERLESS_REPO = "squne121/agent-platform"
_MARKERLESS_ISSUE = 2963
_MARKERLESS_PR = 2967
_MARKERLESS_BODY = """## Allowed Paths
- `.claude/agents/issue-design-reviewer.md`
- `.claude/skills/issue-refinement-loop/references/semantic-design-review.md`
- `.claude/skills/issue-refinement-loop/tests/**`
"""
_MARKERLESS_FILES = [
    {"path": ".claude/agents/issue-design-reviewer.md"},
    {"path": ".claude/skills/issue-refinement-loop/references/semantic-design-review.md"},
    {"path": ".claude/skills/issue-refinement-loop/tests/test_semantic_design_review.py"},
]


def _markerless_glob_run(*, files: list[dict], draft: bool):
    head_sha = "a" * 40
    main_sha = "b" * 40
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            rows = [{"number": _MARKERLESS_PR, "closingIssuesReferences": [{"number": _MARKERLESS_ISSUE}]}]
            return 0, json.dumps(rows), ""
        if argv[:2] == ["gh", "api"] and "timeline" in argv[-1]:
            return 0, "[]", ""
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == str(_MARKERLESS_PR):
            if argv[-1] == _LIVE_CANDIDATE_REFRESH_FIELDS:
                return (
                    0,
                    json.dumps(
                        {
                            "headRefOid": head_sha,
                            "mergedAt": None,
                            "mergeCommit": None,
                            "body": "Closes #2963\n\nMarkerless legacy PR.",
                            "closingIssuesReferences": [{"number": _MARKERLESS_ISSUE}],
                        }
                    ),
                    "",
                )
            return (
                0,
                json.dumps(
                    {
                        "number": _MARKERLESS_PR,
                        "url": f"https://github.com/{_MARKERLESS_REPO}/pull/{_MARKERLESS_PR}",
                        "state": "OPEN",
                        "isDraft": draft,
                        "mergedAt": None,
                        "mergeCommit": None,
                        "headRefOid": head_sha,
                        "closingIssuesReferences": [{"number": _MARKERLESS_ISSUE}],
                        "body": "Closes #2963\n\nMarkerless legacy PR.",
                        "files": files,
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"body": _MARKERLESS_BODY}), ""
        if argv[:2] == ["gh", "api"] and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    return run, calls


def test_markerless_glob_intake_positive(monkeypatch):
    """GIVEN markerless open/draft PRs with all three entries covered WHEN intake runs THEN resume."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_markerless_glob_positive")
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_markerless_glob_positive")
    assert landed_evidence.build_scope_manifest(_MARKERLESS_BODY)["allowed_paths"] == sorted(
        [line[3:-1] for line in _MARKERLESS_BODY.splitlines()[1:]]
    )
    for draft in (False, True):
        run, calls = _markerless_glob_run(files=_MARKERLESS_FILES + [{"path": "unrelated/extra.txt"}], draft=draft)
        evidence = landed_evidence.collect_candidate_inputs(
            repo=_MARKERLESS_REPO, issue_number=_MARKERLESS_ISSUE, current_scope=_MARKERLESS_BODY, run_command=run
        )
        assert len(evidence["candidates"]) == 1
        candidate = evidence["candidates"][0]
        assert candidate["lifecycle"] == ("draft" if draft else "open")
        assert candidate["scope_coverage"]["status"] == "missing_marker"
        assert candidate["current_scope_ownership"] is True
        monkeypatch.setattr(build_capsule, "_run_command", run)
        production = build_capsule._collect_implementation_landed_evidence(
            issue_number=_MARKERLESS_ISSUE,
            repo=_MARKERLESS_REPO,
            issue_body=_MARKERLESS_BODY,
            command_log=[],
            next_action_route="proceed_to_step_1",
        )
        assert production["decision_time_rebind"] == {"status": "fresh"}
        assert production["candidates"][0]["current_scope_ownership"] is True
        assert production["landing_disposition"]["disposition"] == "existing_pr_resume"
        assert production["landing_disposition"]["reason_codes"] == ["markerless_allowed_paths_coverage"]
        assert production["pre_step1_data_plane"] == {"start_data_plane": False, "action": "resume_existing_pr"}
        assert any(argv[:3] == ["gh", "pr", "view"] and "files" in argv[-1] for argv in calls)


def test_markerless_glob_intake_negative(monkeypatch):
    """GIVEN a markerless PR missing only tests/** WHEN intake runs THEN reconciliation, never resume."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_markerless_glob_negative")
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_markerless_glob_negative")
    for draft in (False, True):
        run, calls = _markerless_glob_run(files=_MARKERLESS_FILES[:2], draft=draft)
        evidence = landed_evidence.collect_candidate_inputs(
            repo=_MARKERLESS_REPO, issue_number=_MARKERLESS_ISSUE, current_scope=_MARKERLESS_BODY, run_command=run
        )
        assert evidence["candidates"][0]["scope_coverage"]["status"] == "missing_marker"
        assert evidence["candidates"][0]["current_scope_ownership"] is False
        monkeypatch.setattr(build_capsule, "_run_command", run)
        production = build_capsule._collect_implementation_landed_evidence(
            issue_number=_MARKERLESS_ISSUE,
            repo=_MARKERLESS_REPO,
            issue_body=_MARKERLESS_BODY,
            command_log=[],
            next_action_route="proceed_to_step_1",
        )
        assert production["decision_time_rebind"] == {"status": "fresh"}
        assert production["candidates"][0]["current_scope_ownership"] is False
        assert production["landing_disposition"]["disposition"] == "reconciliation_required"
        assert production["landing_disposition"]["reason_codes"] == ["open_draft_scope_ownership_not_exact"]
        assert production["pre_step1_data_plane"]["start_data_plane"] is False
        assert production["pre_step1_data_plane"]["action"] != "resume_existing_pr"
        assert any(argv[:3] == ["gh", "pr", "view"] and "files" in argv[-1] for argv in calls)


# ---------------------------------------------------------------------------
# #2893: historical merged `later_scope_expansion` candidate (PR #2851) vs a
# unique current exact draft candidate (PR #2888) for target Issue #2843 --
# the real incident shape, driven through the production producer/intake
# path with a fake `gh` transport only (no producer decision logic is
# re-implemented here).
# ---------------------------------------------------------------------------

_INCIDENT_REPO = "squne121/agent-platform"
_INCIDENT_ISSUE = 2843
_INCIDENT_EARLIER_BODY = "## Allowed Paths\n- `.claude/skills/a.py`\n"
_INCIDENT_LIVE_BODY = "## Allowed Paths\n- `.claude/skills/a.py`\n- `.claude/skills/b.py`\n"
_INCIDENT_MERGE_OID = "a" * 40
_INCIDENT_DRAFT_HEAD = "b" * 40


def _incident_markers(landed_evidence) -> tuple[str, str]:
    """Genuine durable markers produced by the production marker builder:
    PR #2851 recorded the earlier scope, PR #2888 the current (expanded)
    scope -- both name the target Issue #2843."""
    merged_marker = landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(
            issue_number=_INCIDENT_ISSUE, issue_body=_INCIDENT_EARLIER_BODY, pr_head_sha=_INCIDENT_MERGE_OID
        )
    )
    draft_marker = landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(
            issue_number=_INCIDENT_ISSUE, issue_body=_INCIDENT_LIVE_BODY, pr_head_sha=_INCIDENT_DRAFT_HEAD
        )
    )
    return merged_marker, draft_marker


def _incident_run(
    landed_evidence,
    main_sha: str,
    *,
    fallback=None,
    ancestry_calls: list | None = None,
    live_overrides: dict | None = None,
):
    """Fake `gh` transport for the #2851 (merged) / #2888 (draft) incident:
    both are `verified_cross_reference` timeline candidates (no
    `closingIssuesReferences`, the draft is `Refs #2843`).

    `live_overrides` (#2893 P2 regressions) maps a PR number string to a dict
    of field overrides applied ONLY to the decision-time
    `gh pr view <N> --json <_LIVE_CANDIDATE_REFRESH_FIELDS>` response (the
    freshness-rebind call shape). The collection-time (full-field) response
    is never overridden, so a test can make the live PR state diverge from
    what the first collection saw."""
    merged_marker, draft_marker = _incident_markers(landed_evidence)
    repo = _INCIDENT_REPO
    timeline = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": number,
                    "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/{number}"},
                    "repository_url": f"https://api.github.com/repos/{repo}",
                }
            },
        }
        for number in (2851, 2888)
    ]
    full = {
        "2851": {
            "number": 2851,
            "url": f"https://github.com/{repo}/pull/2851",
            "state": "MERGED",
            "isDraft": False,
            "mergedAt": "2026-10-01T00:00:00Z",
            "mergeCommit": {"oid": _INCIDENT_MERGE_OID},
            "headRefOid": _INCIDENT_MERGE_OID,
            "closingIssuesReferences": [],
            "body": merged_marker,
            "files": [],
        },
        "2888": {
            "number": 2888,
            "url": f"https://github.com/{repo}/pull/2888",
            "state": "OPEN",
            "isDraft": True,
            "mergedAt": None,
            "mergeCommit": None,
            "headRefOid": _INCIDENT_DRAFT_HEAD,
            "closingIssuesReferences": [],
            "body": "Refs #2843\n\n" + draft_marker,
            "files": [],
        },
    }

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return (
                0,
                json.dumps([{"number": n, "closingIssuesReferences": []} for n in (2851, 2888)]),
                "",
            )
        if argv[:2] == ["gh", "api"] and "timeline" in argv[-1]:
            return 0, json.dumps(timeline), ""
        if argv[:3] == ["gh", "pr", "view"] and argv[3] in full:
            payload = full[argv[3]]
            if argv[-1] == _LIVE_CANDIDATE_REFRESH_FIELDS:
                payload = {
                    key: payload[key]
                    for key in ("headRefOid", "mergedAt", "mergeCommit", "body", "closingIssuesReferences")
                }
                payload.update((live_overrides or {}).get(argv[3], {}))
            return 0, json.dumps(payload), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "compare" in argv[2]:
            if ancestry_calls is not None:
                ancestry_calls.append(argv)
            return 0, "ahead\n", ""
        if argv[:3] == ["gh", "issue", "view"] and "body" in argv and argv[argv.index("--json") + 1] == "body":
            return 0, json.dumps({"body": _INCIDENT_LIVE_BODY}), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        if fallback is not None:
            return fallback(argv)
        return 1, "", "unexpected argv: " + " ".join(argv)

    return run


def test_ac7_incident_shaped_2851_2888_intake_resumes_existing_pr(monkeypatch):
    """#2893 AC7: GIVEN the production-shaped incident (PR #2851 merged
    `later_scope_expansion` + PR #2888 draft `covered_exactly`, both
    `verified_cross_reference`, no closing relation) WHEN the production
    candidate collection and the canonical intake composition
    (`build_intake_capsule.py::_collect_implementation_landed_evidence()`)
    run THEN the draft PR #2888 is the `existing_pr_resume` authority --
    never `qualified_candidate_conflict` -- and the historical PR #2851 is
    retained in the candidate evidence."""
    landed_evidence = _load(LANDED_EVIDENCE, "implementation_landed_evidence_for_2893_integration_test")
    main_sha = "9" * 40
    ancestry_calls: list = []
    run = _incident_run(landed_evidence, main_sha, ancestry_calls=ancestry_calls)

    evidence = landed_evidence.collect_candidate_inputs(
        repo=_INCIDENT_REPO, issue_number=_INCIDENT_ISSUE, current_scope=_INCIDENT_LIVE_BODY, run_command=run
    )
    by_number = {c["pr"]["number"]: c for c in evidence["candidates"]}
    assert set(by_number) == {2851, 2888}
    assert by_number[2851]["lifecycle"] == "merged"
    assert by_number[2851]["scope_coverage"]["status"] == "later_scope_expansion"
    assert by_number[2851]["main_ancestry"] == {"verified": True, "reachable": True}
    assert by_number[2888]["lifecycle"] == "draft"
    assert by_number[2888]["scope_coverage"]["status"] == "covered_exactly"
    assert {c["provenance"]["kind"] for c in evidence["candidates"]} == {"verified_cross_reference"}

    result = landed_evidence.derive_landing_disposition(evidence, repo=_INCIDENT_REPO, issue_number=_INCIDENT_ISSUE)
    assert result["disposition"] == "existing_pr_resume"
    assert result["reason_codes"] == []
    assert result["candidate"]["pr"]["number"] == 2888
    # The historical candidate is excluded from conflict counting only.
    assert [c["pr"]["number"] for c in evidence["candidates"]] == [2851, 2888]

    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_2893_integration_test")
    monkeypatch.setattr(build_capsule, "_run_command", run)
    production_evidence = build_capsule._collect_implementation_landed_evidence(
        issue_number=_INCIDENT_ISSUE,
        repo=_INCIDENT_REPO,
        issue_body=_INCIDENT_LIVE_BODY,
        command_log=[],
        next_action_route="proceed_to_step_1",
    )
    landing = production_evidence["landing_disposition"]
    assert landing["reason_codes"] != ["qualified_candidate_conflict"]
    assert landing["disposition"] == "existing_pr_resume"
    assert landing["candidate"]["pr"]["number"] == 2888
    assert production_evidence["pre_step1_data_plane"]["action"] == "resume_existing_pr"


def test_ac8_intake_capsule_pre_step1_data_plane_resumes_existing_pr(monkeypatch):
    """#2893 AC8: GIVEN the incident shape fed through the PUBLIC
    `build_intake_capsule()` production path (fake transport only) WHEN the
    capsule is built THEN `pre_step1_data_plane` routes `resume_existing_pr`
    (not `suppress_worker_worktree_new_pr`) and the selected candidate is the
    current draft PR #2888."""
    landed_evidence = _load(LANDED_EVIDENCE, "implementation_landed_evidence_for_2893_capsule_test")
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_2893_capsule_test")
    main_sha = "9" * 40
    base_run = _no_candidate_already_satisfied_run(_INCIDENT_LIVE_BODY, main_sha)
    monkeypatch.setattr(
        build_capsule,
        "_run_command",
        _incident_run(landed_evidence, main_sha, fallback=base_run),
    )

    capsule, _artifact, exit_code = build_capsule.build_intake_capsule(
        _INCIDENT_ISSUE,
        _INCIDENT_REPO,
        None,
        include_implementation_landed_evidence=True,
    )

    assert exit_code == 0
    landed = capsule["implementation_landed_evidence"]
    assert landed["landing_disposition"]["disposition"] == "existing_pr_resume"
    assert landed["landing_disposition"]["reason_codes"] == []
    assert landed["landing_disposition"]["candidate"]["pr"]["number"] == 2888
    assert landed["pre_step1_data_plane"]["action"] == "resume_existing_pr"
    assert landed["pre_step1_data_plane"]["start_data_plane"] is False
    assert landed["pre_step1_data_plane"]["action"] != "suppress_worker_worktree_new_pr"
    assert set(landed["pre_step1_data_plane"]) == {"start_data_plane", "action"}


# ---------------------------------------------------------------------------
# #2893 PR #2894 review fix_delta (P2): decision-time live PR semantic
# refresh. Every test below drives the production
# `resolve_landing_disposition_with_freshness_rebind()` composition through
# the fake `gh` transport, where the FIRST collection (full-field
# `gh pr view`) and the decision-time refresh (`headRefOid,mergedAt,...,body,
# closingIssuesReferences`) answer differently via `live_overrides`.
# ---------------------------------------------------------------------------


def _rebind(landed_evidence, run):
    return landed_evidence.resolve_landing_disposition_with_freshness_rebind(
        repo=_INCIDENT_REPO,
        issue_number=_INCIDENT_ISSUE,
        current_scope=_INCIDENT_LIVE_BODY,
        run_command=run,
    )


def _marker_for(landed_evidence, body: str, head: str) -> str:
    return landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(issue_number=_INCIDENT_ISSUE, issue_body=body, pr_head_sha=head)
    )


def test_p2_baseline_refresh_without_overrides_still_resumes_current_draft():
    """Control: with no divergence between collection and decision time the
    carve-out (and the refresh) keep `existing_pr_resume` for PR #2888."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_baseline")
    result = _rebind(landed_evidence, _incident_run(landed_evidence, "9" * 40))
    assert result["decision_time_rebind"] == {"status": "fresh"}
    assert result["landing_disposition"]["disposition"] == "existing_pr_resume"
    assert result["landing_disposition"]["candidate"]["pr"]["number"] == 2888


def test_p2_current_draft_marker_missing_after_collection_does_not_resume_from_stale_exact():
    """GIVEN the draft's marker is exact at collection WHEN its live body no
    longer carries a marker at decision time THEN the stale exact state is
    not used for historical exclusion/resume (fail-closed conflict)."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_draft_marker_missing")
    run = _incident_run(
        landed_evidence, "9" * 40, live_overrides={"2888": {"body": "Refs #2843\n\nmarker removed after collection"}}
    )
    result = _rebind(landed_evidence, run)
    landing = result["landing_disposition"]
    assert landing["disposition"] != "existing_pr_resume"
    assert landing["disposition"] == "reconciliation_required"
    assert landing["reason_codes"] == ["qualified_candidate_conflict"]
    by_number = {c["pr"]["number"]: c for c in result["candidates"]}
    assert by_number[2888]["scope_coverage"]["status"] == "missing_marker"


def test_p2_current_draft_marker_becomes_non_exact_after_collection_does_not_resume():
    """The draft's live marker now records an earlier (non-exact) scope."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_draft_marker_non_exact")
    stale_scope_marker = _marker_for(landed_evidence, _INCIDENT_EARLIER_BODY, _INCIDENT_DRAFT_HEAD)
    run = _incident_run(
        landed_evidence, "9" * 40, live_overrides={"2888": {"body": "Refs #2843\n\n" + stale_scope_marker}}
    )
    landing = _rebind(landed_evidence, run)["landing_disposition"]
    assert landing["disposition"] == "reconciliation_required"
    assert landing["reason_codes"] == ["qualified_candidate_conflict"]


def test_p2_historical_merged_marker_becomes_exact_current_after_collection_is_not_excluded():
    """GIVEN PR #2851 looked historical at collection WHEN its live marker is
    now exact-current THEN it is no longer a historical exclusion: both
    candidates are qualified -> conflict (never `existing_pr_resume`)."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_merged_exact")
    exact_marker = _marker_for(landed_evidence, _INCIDENT_LIVE_BODY, _INCIDENT_MERGE_OID)
    run = _incident_run(landed_evidence, "9" * 40, live_overrides={"2851": {"body": exact_marker}})
    result = _rebind(landed_evidence, run)
    landing = result["landing_disposition"]
    assert landing["disposition"] == "reconciliation_required"
    assert landing["reason_codes"] == ["qualified_candidate_conflict"]
    by_number = {c["pr"]["number"]: c for c in result["candidates"]}
    assert by_number[2851]["scope_coverage"]["status"] == "covered_exactly"


def test_p2_historical_merged_marker_becomes_invalid_after_collection_is_not_excluded():
    """PR #2851's live marker is now malformed (invalid schema_version): it
    must not be excluded as historical."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_merged_invalid")
    merged_marker, _draft_marker = _incident_markers(landed_evidence)
    corrupted = merged_marker.replace("IMPLEMENTATION_SCOPE_COVERAGE_V1\"", "BROKEN_SCHEMA\"", 1)
    assert corrupted != merged_marker
    run = _incident_run(landed_evidence, "9" * 40, live_overrides={"2851": {"body": corrupted}})
    result = _rebind(landed_evidence, run)
    landing = result["landing_disposition"]
    assert landing["disposition"] == "reconciliation_required"
    assert landing["reason_codes"] == ["qualified_candidate_conflict"]
    by_number = {c["pr"]["number"]: c for c in result["candidates"]}
    assert by_number[2851]["scope_coverage"]["status"] == "invalid"


def test_p2_closing_relation_established_at_decision_time_applies_closing_precedence():
    """GIVEN no closing relation at collection WHEN PR #2851's live
    `closingIssuesReferences` now names the target THEN the existing closing
    precedence applies (the same result as a collection-time closing
    candidate): the merged `later_scope_expansion` candidate is the sole
    authority -> `legacy_or_later_scope_expansion`, and the draft no longer
    contributes."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_closing")
    run = _incident_run(
        landed_evidence, "9" * 40, live_overrides={"2851": {"closingIssuesReferences": [{"number": _INCIDENT_ISSUE}]}}
    )
    result = _rebind(landed_evidence, run)
    by_number = {c["pr"]["number"]: c for c in result["candidates"]}
    assert by_number[2851]["provenance"]["kind"] == "closing_relation"
    assert by_number[2888]["provenance"]["kind"] == "verified_cross_reference"
    landing = result["landing_disposition"]
    # Reference: the same evidence with the closing relation present from the
    # start, through the production derive path (not a copy of its logic).
    reference = landed_evidence.derive_landing_disposition(
        result, repo=_INCIDENT_REPO, issue_number=_INCIDENT_ISSUE
    )
    assert landing == reference
    assert landing["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert landing["reason_codes"] == ["legacy_or_later_scope_expansion"]
    assert landing["candidate"]["pr"]["number"] == 2851


def test_p2_closing_relation_on_current_draft_at_decision_time_resumes_via_closing_precedence():
    """The current draft gains a live closing relation: closing precedence
    selects it alone; its exact marker then resumes through the existing path."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_closing_draft")
    run = _incident_run(
        landed_evidence, "9" * 40, live_overrides={"2888": {"closingIssuesReferences": [{"number": _INCIDENT_ISSUE}]}}
    )
    result = _rebind(landed_evidence, run)
    by_number = {c["pr"]["number"]: c for c in result["candidates"]}
    assert by_number[2888]["provenance"]["kind"] == "closing_relation"
    landing = result["landing_disposition"]
    assert landing == landed_evidence.derive_landing_disposition(
        result, repo=_INCIDENT_REPO, issue_number=_INCIDENT_ISSUE
    )
    assert landing["disposition"] == "existing_pr_resume"
    assert landing["candidate"]["pr"]["number"] == 2888


def test_p2_prose_only_pr_body_change_keeps_existing_pr_resume():
    """GIVEN only explanatory prose changes between collection and decision
    time (marker and closing semantics identical) THEN freshness does not
    fail on body bytes and `existing_pr_resume` is kept."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_prose")
    merged_marker, draft_marker = _incident_markers(landed_evidence)
    run = _incident_run(
        landed_evidence,
        "9" * 40,
        live_overrides={
            "2888": {"body": "Refs #2843\n\nPR description reworded after collection.\n\n" + draft_marker},
            "2851": {"body": "Historical PR, wording edited later.\n\n" + merged_marker},
        },
    )
    result = _rebind(landed_evidence, run)
    assert result["decision_time_rebind"] == {"status": "fresh"}
    landing = result["landing_disposition"]
    assert "freshness_rebind_failed" not in landing["reason_codes"]
    assert landing["disposition"] == "existing_pr_resume"
    assert landing["candidate"]["pr"]["number"] == 2888


def test_p2_unverifiable_live_semantic_state_of_participant_fails_closed():
    """A carve-out participant whose live `body` / `closingIssuesReferences`
    cannot be verified is never trusted from the stale collection-time marker:
    `freshness_rebind_failed`."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_unverifiable")
    for number, override in (
        ("2888", {"body": None}),
        ("2888", {"closingIssuesReferences": None}),
        ("2851", {"body": 123}),
        ("2851", {"closingIssuesReferences": "not-a-list"}),
    ):
        run = _incident_run(landed_evidence, "9" * 40, live_overrides={number: override})
        result = _rebind(landed_evidence, run)
        assert result["decision_time_rebind"] == {"status": "stale"}, (number, override)
        assert result["landing_disposition"]["disposition"] == "reconciliation_required"
        assert result["landing_disposition"]["reason_codes"] == ["freshness_rebind_failed"]


def test_p2_participant_is_never_exempted_from_identity_requirement():
    """A carve-out participant that live turns into an identity-mismatch
    marker is NOT added to the #2750 identity exemption: its head drift still
    fails freshness."""
    landed_evidence = _load(LANDED_EVIDENCE, "landed_evidence_p2_identity_not_exempt")
    sibling_marker = landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(
            issue_number=9999, issue_body=_INCIDENT_LIVE_BODY, pr_head_sha="c" * 40
        )
    )
    run = _incident_run(
        landed_evidence,
        "9" * 40,
        live_overrides={"2888": {"body": sibling_marker, "headRefOid": "c" * 40}},
    )
    result = _rebind(landed_evidence, run)
    assert result["landing_disposition"]["disposition"] == "reconciliation_required"
    assert result["landing_disposition"]["reason_codes"] == ["freshness_rebind_failed"]
