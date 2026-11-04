"""GIVEN/WHEN/THEN regression coverage for #2699 landing evidence."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"
spec = importlib.util.spec_from_file_location("implementation_landed_evidence", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ROUTE_SCRIPT = ROOT / ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
route_spec = importlib.util.spec_from_file_location("route_loop_verdict_v2_for_landed_evidence_test", ROUTE_SCRIPT)
assert route_spec and route_spec.loader
route_mod = importlib.util.module_from_spec(route_spec)
sys.modules[route_spec.name] = route_mod
route_spec.loader.exec_module(route_mod)

REPO = "squne121/agent-platform"
ISSUE = 2119
SHA = "a" * 40

# Sentinels for `_build_pipeline_run()`'s sibling live-field overrides
# (research Issue #2761 P1-2 regressions): `_UNSET` means "use the default
# derivation", `_MISSING_FIELD` means "omit the key entirely from the live
# `gh pr view` JSON payload" (distinct from an explicit `None` value, which
# callers pass directly as the override).
_UNSET = object()
_MISSING_FIELD = object()


def _candidate(
    *,
    lifecycle="merged",
    provenance="closing_relation",
    ancestry=True,
    fresh=True,
    ownership=True,
    pr_number=2137,
    scope_coverage=None,
):
    candidate = {
        "target": {"repo": REPO, "issue_number": ISSUE},
        "pr": {"number": pr_number, "url": f"https://github.com/{REPO}/pull/{pr_number}", "head_sha": SHA},
        "provenance": {"kind": provenance, "verified": True},
        "lifecycle": lifecycle,
        "head_fresh": fresh,
        "current_scope_ownership": ownership,
    }
    if lifecycle == "merged":
        candidate.update({"merge_oid": SHA, "main_ancestry": {"verified": ancestry, "reachable": ancestry}})
    if scope_coverage is not None:
        candidate["scope_coverage"] = scope_coverage
    return candidate


def _sibling_identity_mismatch_coverage(*, other_issue_number=9999):
    """A well-formed candidate-local marker whose `issue_number` names a
    *different* Issue, with no other marker error -- the exact #2750 shape
    (`_parse_marker()` returns `errors == ["scope_coverage_issue_identity_mismatch"]`
    alone when every other marker field validates)."""
    return {"status": "invalid", "errors": ["scope_coverage_issue_identity_mismatch"]}


def _evidence(*, candidates, coverage=None, freshness="fresh", contradictory=False, repo=REPO, issue=ISSUE):
    return {
        "schema": mod.EVIDENCE_SCHEMA,
        "schema_version": 1,
        "target": {
            "repo": repo,
            "issue_number": issue,
            "body_sha256": "sha256:" + "b" * 64,
        },
        "freshness": {"status": freshness, "observed_at": "2026-09-21T00:00:00Z", "max_age_seconds": 300},
        "contradictory": contradictory,
        "candidates": candidates,
        "scope_coverage": coverage,
    }


def _exact_coverage():
    return mod.normalize_scope_coverage({"ac": ["AC1", "AC2"]}, {"ac": ["AC2", "AC1"]})


def test_validator_rejects_missing_stale_and_identity_mismatched_evidence():
    """GIVEN malformed or stale evidence WHEN validated THEN it cannot land."""
    for raw in (None, _evidence(candidates=[], freshness="stale"), _evidence(candidates=[], repo="other/repo")):
        result = mod.derive_landing_disposition(raw, repo=REPO, issue_number=ISSUE)
        assert result["disposition"] == "reconciliation_required"


def test_scope_normalizer_distinguishes_exact_coverage_from_later_expansion():
    """GIVEN immutable and live scopes WHEN normalized THEN expansion is explicit."""
    exact = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1"]})
    expanded = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1", "AC2"]})
    assert exact["exact_coverage"] is True
    assert expanded["later_scope_expansion"] is True
    assert exact["immutable_merged_snapshot"]["content_sha256"] != expanded["live_current_scope"]["content_sha256"]


def _scope_manifest_issue_body(
    *, reversed_lists: bool = False, operational: str = "progress one", ac_checked: bool = False
) -> str:
    """An Issue body containing both semantic fields (In Scope / Acceptance
    Criteria / Allowed Paths) and the operational-prose / progress-state /
    runtime-evidence sections `build_scope_manifest()` must exclude."""
    in_scope = "- build intake\n- publish marker" if not reversed_lists else "- publish marker\n- build intake"
    ac1_box = "[x]" if ac_checked else "[ ]"
    ac2_box = "[ ]" if ac_checked else "[x]"
    acs = (
        f"- {ac2_box} AC2: marker persists\n- {ac1_box} AC1: scope normalizes"
        if not reversed_lists
        else f"- {ac1_box} AC1: scope normalizes\n- {ac2_box} AC2: marker persists"
    )
    paths = "- `.claude/a.py`\n- `.claude/b.py`" if not reversed_lists else "- `.claude/b.py`\n- `.claude/a.py`"
    return f"""## Machine-Readable Contract
```yaml
goal_ref: marker goal
change_kind: workflow
```
## In Scope
{in_scope}
## Acceptance Criteria
{acs}
## Allowed Paths
{paths}
## Remaining Parent Gaps
- {operational}
## Runtime Evidence
- changing this does not alter semantic scope
"""


def test_build_scope_manifest_no_path_marker_returns_empty():
    """AC12 (Issue #2783): canonical marker `(none)` and legacy marker
    `読み取り専用。リポジトリ変更なし（既定）` as the sole Allowed Paths entry
    both resolve `allowed_paths` to [] (this filtering is Allowed-Paths
    specific -- In Scope / Acceptance Criteria are unaffected)."""
    body_canonical = """## Machine-Readable Contract
```yaml
goal_ref: marker goal
change_kind: workflow
```
## In Scope
- build intake
## Acceptance Criteria
- [ ] AC1: scope normalizes
## Allowed Paths
- (none)
"""
    manifest_canonical = mod.build_scope_manifest(body_canonical)
    assert manifest_canonical["allowed_paths"] == []
    assert manifest_canonical["in_scope"] == ["build intake"]

    body_legacy = """## Machine-Readable Contract
```yaml
goal_ref: marker goal
change_kind: workflow
```
## In Scope
- build intake
## Acceptance Criteria
- [ ] AC1: scope normalizes
## Allowed Paths
- 読み取り専用。リポジトリ変更なし（既定）
"""
    manifest_legacy = mod.build_scope_manifest(body_legacy)
    assert manifest_legacy["allowed_paths"] == []


def test_scope_normalizer_excludes_operational_prose_and_progress_state():
    """AC2: the canonical normalizer includes only the "Canonical
    Implementation Scope Normalizer" semantic fields (goal_ref / change_kind
    / In Scope / Acceptance Criteria text / Allowed Paths), excludes
    operational prose / checkbox state / runtime evidence / progress text,
    and is order-independent (sorted) per list -- none of these alone may
    change the digest, while a genuine semantic change must."""
    baseline = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(operational="first update"))
    )

    # Checkbox state ([ ] vs [x], per item) alone must not change the digest.
    checkbox_flip = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(operational="first update", ac_checked=True))
    )
    assert checkbox_flip["content_sha256"] == baseline["content_sha256"]

    # "## Remaining Parent Gaps" operational/progress prose alone must not
    # change the digest.
    operational_only = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(operational="a completely different later update"))
    )
    assert operational_only["content_sha256"] == baseline["content_sha256"]

    # Recording order of the In Scope / Acceptance Criteria / Allowed Paths
    # bullets alone must not change the digest (order-independent sort
    # normalization).
    reordered = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(reversed_lists=True, operational="first update"))
    )
    assert reordered["content_sha256"] == baseline["content_sha256"]

    # "## Runtime Evidence" content is excluded entirely.
    body_with_extra_runtime_evidence = (
        _scope_manifest_issue_body(operational="first update") + "- another runtime detail\n"
    )
    runtime_extra = mod.canonicalize_scope_manifest(mod.build_scope_manifest(body_with_extra_runtime_evidence))
    assert runtime_extra["content_sha256"] == baseline["content_sha256"]

    # Sanity: a genuine semantic change (a new Allowed Paths entry) DOES
    # change the digest -- the exclusions above are not vacuously trivial.
    manifest = mod.build_scope_manifest(_scope_manifest_issue_body(operational="first update"))
    manifest["allowed_paths"] = manifest["allowed_paths"] + [".claude/c.py"]
    changed = mod.canonicalize_scope_manifest(manifest)
    assert changed["content_sha256"] != baseline["content_sha256"]

    # Only the semantic fields the normalizer section lists are present.
    assert set(mod.build_scope_manifest(_scope_manifest_issue_body()).keys()) == {
        "schema_version",
        "goal_ref",
        "change_kind",
        "in_scope",
        "acceptance_criteria",
        "allowed_paths",
    }


def test_disposition_precedence_reconciles_contradictory_insufficient_stale_and_identity_mismatch():
    """GIVEN unsafe evidence WHEN routed THEN reconciliation wins all lifecycle paths."""
    cases = [
        _evidence(candidates=[_candidate()], coverage=_exact_coverage(), contradictory=True),
        _evidence(candidates=[_candidate(ancestry=False)], coverage=_exact_coverage()),
        _evidence(candidates=[_candidate()], coverage=_exact_coverage(), freshness="stale"),
        _evidence(candidates=[_candidate()], coverage=_exact_coverage(), issue=9999),
    ]
    assert all(
        mod.derive_landing_disposition(case, repo=REPO, issue_number=ISSUE)["disposition"] == "reconciliation_required"
        for case in cases
    )


def test_open_draft_closed_unmerged_and_no_candidate_dispositions():
    """GIVEN candidate lifecycle states WHEN routed THEN only verified resumable PRs resume."""
    assert (
        mod.derive_landing_disposition(
            _evidence(candidates=[_candidate(lifecycle="open")]), repo=REPO, issue_number=ISSUE
        )["disposition"]
        == "existing_pr_resume"
    )
    assert (
        mod.derive_landing_disposition(
            _evidence(candidates=[_candidate(lifecycle="draft")]), repo=REPO, issue_number=ISSUE
        )["disposition"]
        == "existing_pr_resume"
    )
    assert (
        mod.derive_landing_disposition(
            _evidence(candidates=[_candidate(lifecycle="closed_unmerged")]), repo=REPO, issue_number=ISSUE
        )["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )
    assert (
        mod.derive_landing_disposition(_evidence(candidates=[]), repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )


def test_candidate_discovery_is_not_landing_authority():
    """GIVEN generic refs/title-like rows WHEN discovered THEN they never produce a candidate."""
    rows = [{"number": 1, "body": "Refs #2119", "state": "MERGED", "mergedAt": "x", "mergeCommit": {"oid": SHA}}]

    def run(argv):
        return 0, __import__("json").dumps(rows), ""

    evidence = mod.collect_candidate_inputs(repo=REPO, issue_number=ISSUE, current_scope={}, run_command=run)
    # A generic Refs form is not a verified cross-reference.
    assert evidence["candidates"] == []
    assert (
        mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )


def test_2119_2137_non_closing_and_merged_closing_regressions():
    """GIVEN verified ancestry and exact scope WHEN relation differs THEN both qualify only with full evidence."""
    non_closing = _evidence(candidates=[_candidate(provenance="verified_cross_reference")], coverage=_exact_coverage())
    closing = _evidence(candidates=[_candidate(provenance="closing_relation")], coverage=_exact_coverage())
    assert (
        mod.derive_landing_disposition(non_closing, repo=REPO, issue_number=ISSUE)["disposition"]
        == "implementation_already_landed"
    )
    assert (
        mod.derive_landing_disposition(closing, repo=REPO, issue_number=ISSUE)["disposition"]
        == "implementation_already_landed"
    )


def test_lifecycle_and_scope_expansion_regressions():
    """GIVEN historical merged work and expanded scope WHEN routed THEN a new implementation remains allowed."""
    expanded = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1", "AC9"]})
    result = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate()], coverage=expanded), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"


def test_candidate_discovery_dedupes_and_reconciles_conflicting_qualified_candidates():
    """AC7: two independently qualified PRs are never selected first-hit."""
    one = _candidate(lifecycle="open")
    two = _candidate(lifecycle="draft")
    result = mod.derive_landing_disposition(_evidence(candidates=[one, two]), repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "reconciliation_required"
    assert result["reason_codes"] == ["qualified_candidate_conflict"]

    # Closing relation wins over an independently verified non-closing source;
    # only same-authority multiplicity is contradictory.
    closing = _candidate(lifecycle="open", provenance="closing_relation")
    cross_ref = _candidate(lifecycle="draft", provenance="verified_cross_reference")
    result = mod.derive_landing_disposition(_evidence(candidates=[closing, cross_ref]), repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "existing_pr_resume"


def test_markerless_plain_entry_legacy():
    """GIVEN wildcard-free entries WHEN matching THEN exact and directory descendants retain legacy coverage."""
    entry = "src/ui"
    for path in ("src/ui", "src/ui/panel.py", "src/ui/deep/panel.py"):
        assert mod._allowed_paths_covered([entry], [{"path": path}]) is True
    for path in ("src/uix/panel.py", "src/ui.py", "src/other/panel.py"):
        assert mod._allowed_paths_covered([entry], [{"path": path}]) is False
    assert mod._allowed_paths_covered(["./src/ui/"], [{"path": "./src/ui/panel.py"}]) is True
    # The shared matcher's literal-only exact semantics must NOT replace legacy prefix semantics.
    assert mod._allowed_paths_covered(["src/ui"], [{"path": "src/ui/panel.py"}]) is True


def test_markerless_wildcard_safe_subset():
    """GIVEN explicit glob entries WHEN matching THEN the shared segment grammar covers their paths."""
    cases = (
        ("src/tests/**", "src/tests/deep/check.py"),
        ("src/tests/**", "src/tests"),  # '**' matches zero segments.
        ("src/*/check.py", "src/unit/check.py"),
        ("src/**/check.py", "src/check.py"),
        ("src/**/check.py", "src/a/b/check.py"),
        ("./src/**/check.py", "./src/a/check.py"),
    )
    for entry, path in cases:
        assert mod._allowed_paths_covered([entry], [{"path": path}]) is True, (entry, path)


def test_markerless_wildcard_fail_closed():
    """GIVEN invalid patterns, invalid files, or near misses WHEN matching THEN deny coverage."""
    misses = (
        ("src/tests/**", "src/tests-other/check.py"),
        ("src/*/check.py", "src/check.py"),
        ("src/*/check.py", "src/a/b/check.py"),
        ("src/**/check.py", "src/check.py.bak"),
        ("src/*.py", "src/check.py"),
        ("src/test*", "src/tests"),
        ("src/***/check.py", "src/a/check.py"),
        ("../src/**", "src/check.py"),
        ("/src/**", "src/check.py"),
        ("src//**", "src/check.py"),
        ("src/**/", "src/check.py"),
        ("src/tests/**", "../src/tests/check.py"),
        ("src/tests/**", "/src/tests/check.py"),
        ("src/tests/**", "src/tests/../check.py"),
        ("src/tests/**", "src/tests//check.py"),
        ("src/tests/**", "src\\tests\\check.py"),
        ("src/tests", "../src/tests/check.py"),
        ("src/tests", "/src/tests/check.py"),
        ("../src/tests", "src/tests/check.py"),
    )
    for entry, path in misses:
        assert mod._allowed_paths_covered([entry], [{"path": path}]) is False, (entry, path)
    assert mod._allowed_paths_covered(["src/tests/**"], None) is False
    assert mod._allowed_paths_covered(["src/tests/**"], [{"path": None}]) is False


def test_markerless_all_of_coverage():
    """GIVEN multiple Allowed Paths WHEN one is absent THEN coverage fails, not PR file allowlisting."""
    entries = ["src/core.py", "src/docs", "src/tests/**"]
    files = [
        {"path": "src/core.py"},
        {"path": "src/docs/guide.md"},
        {"path": "src/tests/unit/test_core.py"},
    ]
    assert mod._allowed_paths_covered(entries, files) is True
    assert mod._allowed_paths_covered(entries, files + [{"path": "unrelated/extra.txt"}]) is True
    for missing in range(len(files)):
        assert mod._allowed_paths_covered(entries, files[:missing] + files[missing + 1 :]) is False
    assert mod._allowed_paths_covered([], files) is False
    assert mod._allowed_paths_covered(entries, []) is False
    assert mod._allowed_paths_covered(entries + ["../invalid"], files) is False


def test_markerless_open_draft_candidate_allowed_paths_coverage_determines_resume_or_reconciliation():
    """AC6: legacy resumable branches need explicit current Allowed Paths coverage."""
    resumable = _candidate(lifecycle="open", fresh=True, ownership=True)
    blocked = _candidate(lifecycle="draft", fresh=True, ownership=False)
    assert (
        mod.derive_landing_disposition(_evidence(candidates=[resumable]), repo=REPO, issue_number=ISSUE)["disposition"]
        == "existing_pr_resume"
    )
    assert (
        mod.derive_landing_disposition(_evidence(candidates=[blocked]), repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )


def test_candidate_discovery_is_not_landing_authority_or_body_heuristic():
    """AC8: Refs/title text cannot masquerade as timeline PR provenance."""
    rows = [{"number": 1, "body": "Refs #2119", "state": "MERGED", "mergedAt": "x", "mergeCommit": {"oid": SHA}}]

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, __import__("json").dumps(rows), ""
        return 0, "[]", ""

    evidence = mod.collect_candidate_inputs(repo=REPO, issue_number=ISSUE, current_scope={}, run_command=run)
    assert evidence["candidates"] == []


def test_disposition_precedence_reuses_already_satisfied_before_landed_evidence():
    """AC4: a no-landing-authority result (no qualified candidate, or only
    closed-unmerged candidates survive qualification) falls back to #2607's
    existing already_satisfied early-exit instead of a new enum. Every other
    disposition (reconciliation_required / implementation_already_landed /
    existing_pr_resume / any other ordinary_dispatch reason) is untouched --
    Disposition Precedence step 1 always wins over step 2."""
    no_candidate = mod.derive_landing_disposition(_evidence(candidates=[]), repo=REPO, issue_number=ISSUE)
    assert no_candidate["reason_codes"] == ["no_qualified_candidate"]

    closed_unmerged = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate(lifecycle="closed_unmerged")]), repo=REPO, issue_number=ISSUE
    )
    assert closed_unmerged["reason_codes"] == ["closed_unmerged_candidate"]

    for landing_result in (no_candidate, closed_unmerged):
        composed = mod.apply_already_satisfied_precedence(
            landing_result,
            next_action_route="proceed_to_step_1",
            product_spec_routing_action="continue",
            pr_exists=False,
            base_ac_satisfied=True,
            route_loop_verdict_v2_module=route_mod,
        )
        assert composed["disposition"] == "already_satisfied"
        # Reuses route_loop_verdict_v2's existing production decision
        # function rather than re-implementing the early-exit判定 (#2607).
        assert composed["already_satisfied_decision"] == route_mod.resolve_already_satisfied_early_exit_decision(
            next_action_route="proceed_to_step_1",
            product_spec_routing_action="continue",
            pr_exists=False,
            base_ac_satisfied=True,
        )

    # base_ac_satisfied False must not fire the fallback.
    untouched = mod.apply_already_satisfied_precedence(
        no_candidate,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=False,
        route_loop_verdict_v2_module=route_mod,
    )
    assert untouched == no_candidate

    # Step 1 always wins: reconciliation_required is never overridden, even
    # when base_ac_satisfied is independently true.
    reconciliation = mod.derive_landing_disposition(None, repo=REPO, issue_number=ISSUE)
    passthrough = mod.apply_already_satisfied_precedence(
        reconciliation,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=True,
        route_loop_verdict_v2_module=route_mod,
    )
    assert passthrough == reconciliation

    # implementation_already_landed / existing_pr_resume are also untouched.
    landed = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate(lifecycle="merged")], coverage=_exact_coverage()),
        repo=REPO,
        issue_number=ISSUE,
    )
    resume = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate(lifecycle="open")]), repo=REPO, issue_number=ISSUE
    )
    for result in (landed, resume):
        assert (
            mod.apply_already_satisfied_precedence(
                result,
                next_action_route="proceed_to_step_1",
                product_spec_routing_action="continue",
                pr_exists=False,
                base_ac_satisfied=True,
                route_loop_verdict_v2_module=route_mod,
            )
            == result
        )


def test_legacy_merged_candidate_without_durable_marker_never_yields_implementation_already_landed():
    """AC5: a merged candidate with a missing durable marker (legacy PR,
    e.g. #2137) must never yield implementation_already_landed, for either
    provenance kind, and must not silently reconstruct merge-time scope from
    the current body."""
    missing_marker_coverage = mod.coverage_from_pr_body(
        pr_body="no marker here", issue_number=ISSUE, live_issue_body="## Allowed Paths\n- `.claude/a.py`\n"
    )
    assert missing_marker_coverage["status"] == "missing_marker"

    for provenance in ("closing_relation", "verified_cross_reference"):
        candidate = _candidate(lifecycle="merged", provenance=provenance)
        evidence = _evidence(candidates=[candidate], coverage=missing_marker_coverage)
        result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
        assert result["disposition"] != "implementation_already_landed"
        assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"
        assert result["reason_codes"] == ["legacy_or_later_scope_expansion"]


def test_2119_2137_legacy_and_durable_marker_fixture_regressions():
    """AC10: #2119/PR#2137-shaped fixtures pin every required lifecycle
    disposition, including the legacy (marker-missing) vs durable-marker
    merged-closing distinction, scope expansion, open/draft resume (marker
    and markerless), closed-unmerged, no-candidate, contradictory,
    insufficient evidence, and a malformed merged marker."""
    legacy_missing_marker = mod.coverage_from_pr_body(pr_body="", issue_number=ISSUE, live_issue_body="body")
    legacy = _evidence(
        candidates=[_candidate(lifecycle="merged", provenance="closing_relation")], coverage=legacy_missing_marker
    )
    assert (
        mod.derive_landing_disposition(legacy, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    durable = _evidence(
        candidates=[_candidate(lifecycle="merged", provenance="closing_relation")], coverage=_exact_coverage()
    )
    assert (
        mod.derive_landing_disposition(durable, repo=REPO, issue_number=ISSUE)["disposition"]
        == "implementation_already_landed"
    )

    expansion = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1", "AC9"]})
    expanded = _evidence(candidates=[_candidate(lifecycle="merged")], coverage=expansion)
    assert (
        mod.derive_landing_disposition(expanded, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    open_marker = _evidence(candidates=[_candidate(lifecycle="open")], coverage=_exact_coverage())
    assert (
        mod.derive_landing_disposition(open_marker, repo=REPO, issue_number=ISSUE)["disposition"]
        == "existing_pr_resume"
    )

    open_markerless = _evidence(candidates=[_candidate(lifecycle="open", ownership=True, fresh=True)])
    assert (
        mod.derive_landing_disposition(open_markerless, repo=REPO, issue_number=ISSUE)["disposition"]
        == "existing_pr_resume"
    )

    draft_markerless_blocked = _evidence(candidates=[_candidate(lifecycle="draft", ownership=False, fresh=True)])
    assert (
        mod.derive_landing_disposition(draft_markerless_blocked, repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )

    closed_unmerged = _evidence(candidates=[_candidate(lifecycle="closed_unmerged")])
    assert (
        mod.derive_landing_disposition(closed_unmerged, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    no_candidate = _evidence(candidates=[])
    assert (
        mod.derive_landing_disposition(no_candidate, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    contradictory = _evidence(
        candidates=[_candidate(lifecycle="merged")], coverage=_exact_coverage(), contradictory=True
    )
    assert (
        mod.derive_landing_disposition(contradictory, repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )

    assert (
        mod.derive_landing_disposition(None, repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )

    # #2699 P1-1: a malformed merged-candidate marker is reconciliation_required
    # regardless of lifecycle, ahead of the ancestry/exact-coverage checks.
    malformed_merged_marker = {"status": "invalid", "errors": ["scope_coverage_manifest_digest_mismatch"]}
    malformed = _evidence(candidates=[_candidate(lifecycle="merged")], coverage=malformed_merged_marker)
    malformed_result = mod.derive_landing_disposition(malformed, repo=REPO, issue_number=ISSUE)
    assert malformed_result["disposition"] == "reconciliation_required"
    assert malformed_result["reason_codes"] == ["scope_coverage_manifest_digest_mismatch"]


def test_decision_time_freshness_rebind_and_bounded_retry():
    """AC9: disposition finalization re-verifies issue body sha256 /
    candidate head-or-merge-oid / current main sha live, immediately before
    finalizing, bounded-retries collection exactly once on drift, and
    returns reconciliation_required(freshness_rebind_failed) if the retry
    still disagrees."""
    issue_body = "## Allowed Paths\n- `.claude/a.py`\n"
    allowed_files = [{"path": ".claude/a.py"}]

    def _pr_list_response():
        return 0, json.dumps([{"number": 2137, "closingIssuesReferences": [{"number": ISSUE}]}]), ""

    def _pr_view_full_response():
        return (
            0,
            json.dumps(
                {
                    "number": 2137,
                    "url": f"https://github.com/{REPO}/pull/2137",
                    "state": "OPEN",
                    "isDraft": False,
                    "mergedAt": None,
                    "mergeCommit": None,
                    "headRefOid": SHA,
                    "closingIssuesReferences": [{"number": ISSUE}],
                    "body": "",
                    "files": allowed_files,
                }
            ),
            "",
        )

    def _pr_view_identity_response():
        return (
            0,
            json.dumps(
                {
                    "headRefOid": SHA,
                    "mergedAt": None,
                    "mergeCommit": None,
                    "body": "",
                    "closingIssuesReferences": [{"number": ISSUE}],
                }
            ),
            "",
        )

    def make_run(main_sha_sequence):
        state = {"main_sha_calls": 0, "pr_list_calls": 0}

        def run(argv):
            if argv[:3] == ["gh", "pr", "list"]:
                state["pr_list_calls"] += 1
                return _pr_list_response()
            if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
                return 0, "[]", ""
            if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
                sha = main_sha_sequence[state["main_sha_calls"]]
                state["main_sha_calls"] += 1
                return 0, sha, ""
            if argv[:3] == ["gh", "pr", "view"]:
                if argv[-1] == mod._LIVE_CANDIDATE_REFRESH_FIELDS:
                    return _pr_view_identity_response()
                return _pr_view_full_response()
            if argv[:3] == ["gh", "issue", "view"]:
                return 0, json.dumps({"body": issue_body}), ""
            return 1, "", "unexpected argv: " + " ".join(argv)

        return run, state

    # Drift on the first rebind check, then a matching retry: succeeds using
    # the retried (second) collection's disposition.
    main_shas_recover = ["1" * 40, "2" * 40, "3" * 40, "3" * 40]
    run_recover, state_recover = make_run(main_shas_recover)
    result_recover = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=issue_body, run_command=run_recover
    )
    assert result_recover["decision_time_rebind"]["status"] == "fresh"
    assert result_recover["landing_disposition"]["disposition"] == "existing_pr_resume"
    # Bounded: exactly one initial collection + one retry collection.
    assert state_recover["pr_list_calls"] == 2

    # Drift on both the first and the (bounded, single) retry: gives up.
    main_shas_fail = ["1" * 40, "2" * 40, "3" * 40, "4" * 40]
    run_fail, state_fail = make_run(main_shas_fail)
    result_fail = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=issue_body, run_command=run_fail
    )
    assert result_fail["decision_time_rebind"]["status"] == "stale"
    assert result_fail["landing_disposition"] == {
        "disposition": "reconciliation_required",
        "reason_codes": ["freshness_rebind_failed"],
        "candidate": None,
    }
    assert state_fail["pr_list_calls"] == 2


def test_default_route_loop_verdict_v2_loader_succeeds_without_module_injection():
    """AC4: GIVEN no `route_loop_verdict_v2_module` injection WHEN the
    default (dependency-injection-free) `_load_route_loop_verdict_v2_module()`
    loader runs THEN it actually loads `route_loop_verdict_v2.py` (returns a
    non-None module exposing the production decision function) instead of
    silently swallowing the `sys.modules`-registration-order exec failure
    and returning None."""
    loaded = mod._load_route_loop_verdict_v2_module()
    assert loaded is not None
    assert hasattr(loaded, "resolve_already_satisfied_early_exit_decision")
    assert hasattr(loaded, "resolve_pre_step1_data_plane_action")


def test_apply_already_satisfied_precedence_fires_through_default_loader():
    """AC3/AC4: GIVEN a no-landing-authority result WHEN
    `apply_already_satisfied_precedence()` is called WITHOUT injecting
    `route_loop_verdict_v2_module` (default loader only) THEN it still
    successfully loads `route_loop_verdict_v2.py` and fires the
    `already_satisfied` composition -- proving the default production load
    path (not just the dependency-injected test path) actually applies the
    precedence."""
    no_candidate = mod.derive_landing_disposition(_evidence(candidates=[]), repo=REPO, issue_number=ISSUE)
    composed = mod.apply_already_satisfied_precedence(
        no_candidate,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="not_yet_evaluated_at_pre_step1_landing_disposition",
        pr_exists=False,
        base_ac_satisfied=True,
    )
    assert composed["disposition"] == "already_satisfied"
    assert composed["reason_codes"] == ["already_satisfied_no_pr_created"]


def _patch_spec_from_file_location_to_fail(monkeypatch, expected_name):
    """Return a REAL `ModuleSpec` (built via the genuine
    `spec_from_file_location()`) so `module_from_spec()` / the import
    machinery see a fully well-formed spec, with only `loader.exec_module()`
    swapped out to raise -- isolates the `exec_module()` failure path
    without hand-rolling a fake spec/loader pair that the frozen import
    machinery rejects for missing attributes (`origin`,
    `submodule_search_locations`, etc.)."""
    import importlib.util as importlib_util

    original_spec_from_file_location = importlib_util.spec_from_file_location

    def fake_spec_from_file_location(name, path):
        assert name == expected_name
        spec = original_spec_from_file_location(name, path)

        def failing_exec_module(module):  # noqa: ARG001 - matches Loader.exec_module signature
            raise RuntimeError("simulated exec_module failure")

        spec.loader.exec_module = failing_exec_module
        return spec

    monkeypatch.setattr(importlib_util, "spec_from_file_location", fake_spec_from_file_location)


def test_default_adjudicate_vc_result_loader_succeeds_without_module_injection():
    """#2713 AC9: GIVEN no injection WHEN the default
    `_load_adjudicate_vc_result_module()` loader runs THEN it actually loads
    `adjudicate_vc_result.py` and exposes
    `adapt_test_verdict_to_current_vc_result()` (the function
    `derive_base_ac_satisfied_from_verification_result()` reuses)."""
    loaded = mod._load_adjudicate_vc_result_module()
    assert loaded is not None
    assert hasattr(loaded, "adapt_test_verdict_to_current_vc_result")


def test_route_loop_verdict_v2_loader_success_leaves_module_registered_under_its_own_name():
    """#2713 AC10: the success path is unaffected by the restore-on-failure
    logic added for AC10 -- the freshly executed module stays registered
    under its own `sys.modules` name."""
    module_name = "route_loop_verdict_v2_for_evidence"
    sys.modules.pop(module_name, None)
    try:
        loaded = mod._load_route_loop_verdict_v2_module()
        assert loaded is not None
        assert sys.modules.get(module_name) is loaded
    finally:
        sys.modules.pop(module_name, None)


def test_route_loop_verdict_v2_loader_removes_only_its_own_newly_inserted_entry_on_failure(monkeypatch):
    """#2713 AC10 (PR #2741 review, P2): WHEN `sys.modules` has NO prior
    entry under the loader's module name AND `exec_module()` fails THEN the
    loader pop()s the entry it itself inserted -- no dangling half-
    initialized module is left behind."""
    module_name = "route_loop_verdict_v2_for_evidence"
    sys.modules.pop(module_name, None)

    _patch_spec_from_file_location_to_fail(monkeypatch, module_name)

    loaded = mod._load_route_loop_verdict_v2_module()
    assert loaded is None
    assert module_name not in sys.modules


def test_route_loop_verdict_v2_loader_restores_prior_entry_on_failure(monkeypatch):
    """#2713 AC10 (PR #2741 review, P2): WHEN `sys.modules` ALREADY has an
    entry under the loader's module name (from an unrelated prior load)
    AND `exec_module()` fails THEN that prior entry is restored -- it must
    never be left deleted or permanently clobbered by the half-initialized
    failed module."""
    module_name = "route_loop_verdict_v2_for_evidence"
    sentinel = types.ModuleType(module_name)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = sentinel
    try:
        _patch_spec_from_file_location_to_fail(monkeypatch, module_name)

        loaded = mod._load_route_loop_verdict_v2_module()
        assert loaded is None
        assert sys.modules.get(module_name) is sentinel
    finally:
        if previous is not None:
            sys.modules[module_name] = previous
        else:
            sys.modules.pop(module_name, None)


def test_adjudicate_vc_result_loader_removes_only_its_own_newly_inserted_entry_on_failure(monkeypatch):
    """#2713 AC10 (PR #2741 review, P2): same register-before-exec /
    restore-on-failure contract as `_load_route_loop_verdict_v2_module()`,
    applied to `_load_adjudicate_vc_result_module()` -- no prior entry
    means the newly inserted entry is simply removed on failure."""
    module_name = "adjudicate_vc_result_for_evidence"
    sys.modules.pop(module_name, None)

    _patch_spec_from_file_location_to_fail(monkeypatch, module_name)

    loaded = mod._load_adjudicate_vc_result_module()
    assert loaded is None
    assert module_name not in sys.modules


def test_adjudicate_vc_result_loader_restores_prior_entry_on_failure(monkeypatch):
    """#2713 AC10 (PR #2741 review, P2): a pre-existing `sys.modules` entry
    under `_load_adjudicate_vc_result_module()`'s module name is restored,
    not destroyed, when `exec_module()` fails."""
    module_name = "adjudicate_vc_result_for_evidence"
    sentinel = types.ModuleType(module_name)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = sentinel
    try:
        _patch_spec_from_file_location_to_fail(monkeypatch, module_name)

        loaded = mod._load_adjudicate_vc_result_module()
        assert loaded is None
        assert sys.modules.get(module_name) is sentinel
    finally:
        if previous is not None:
            sys.modules[module_name] = previous
        else:
            sys.modules.pop(module_name, None)


def test_derive_pr_exists_from_landing_candidate_across_pr_fixture_shapes():
    """#2713 AC3: `pr_exists` production source. open/draft/merged candidates
    are resume/conflict-relevant targets (True); a closed-unmerged candidate
    was abandoned without landing and is not (False); no candidate at all is
    also not (False)."""
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="open")) is True
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="draft")) is True
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="merged")) is True
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="closed_unmerged")) is False
    assert mod.derive_pr_exists_from_landing_candidate(None) is False


def _runtime_ac_entry(ac="AC1", *, command_hash=None, status="pass", exit_code=0, **flags):
    entry = {
        "ac": ac,
        "command_hash": command_hash or ("sha256:" + "1" * 64),
        "status": status,
        "exit_code": exit_code,
    }
    entry.update(flags)
    return entry


def _test_verdict(entries, **overrides):
    payload = {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "generated_at": "2026-09-24T00:00:00Z",
        "head_sha": SHA,
        "contract_body_sha256": "sha256:" + "c" * 64,
        "result": "PASS",
        "runtime_ac_results": entries,
    }
    payload.update(overrides)
    return payload


def test_derive_base_ac_satisfied_from_verification_result_reuses_adjudicate_vc_result_adapter():
    """#2713 AC3/AC9 (PR #2741 review, P1-2): `base_ac_satisfied` production
    source reuses `adjudicate_vc_result.py::adapt_test_verdict_to_current_vc_result()`
    rather than a second, weaker validator. A fresh (head_sha-matching),
    fully clean all-pass TEST_VERDICT_MACHINE/v2 yields True; a fresh result
    with any non-pass entry yields False; anything undeterminable (missing
    result, missing live_main_sha, stale/mismatched head_sha, or malformed
    runtime_ac_results/adapter errors) yields None -- never a fabricated
    True/False fallback."""
    fresh_pass = _test_verdict([_runtime_ac_entry("AC1"), _runtime_ac_entry("AC2", command_hash="sha256:" + "2" * 64)])
    fresh_fail = _test_verdict(
        [_runtime_ac_entry("AC1"), _runtime_ac_entry("AC2", command_hash="sha256:" + "2" * 64, status="fail")]
    )
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_pass, live_main_sha=SHA) is True
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_fail, live_main_sha=SHA) is False

    # Undeterminable cases -- never fabricated.
    assert mod.derive_base_ac_satisfied_from_verification_result(None, live_main_sha=SHA) is None
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_pass, live_main_sha=None) is None
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_pass, live_main_sha="b" * 40) is None
    assert mod.derive_base_ac_satisfied_from_verification_result({"head_sha": SHA}, live_main_sha=SHA) is None
    assert (
        mod.derive_base_ac_satisfied_from_verification_result(
            _test_verdict([], head_sha=SHA), live_main_sha=SHA
        )
        is None
    )


def test_derive_base_ac_satisfied_from_verification_result_rejects_incomplete_payloads():
    """#2713 AC9 (PR #2741 review, P1-2): an incomplete `runtime_ac_results`
    entry (missing AC identity, missing command_hash, exit_code != 0,
    fallback_detected, human_review_required, stop_condition_triggered, or a
    SKIP/PARTIAL status) must never be mistaken for `base_ac_satisfied:
    True`."""
    # Not even a recognizable TEST_VERDICT_MACHINE/v2 payload at all (no
    # `schema` field) -- the adapter fails closed with
    # unsupported_source_schema, which this function treats as
    # undeterminable (None), never True.
    assert (
        mod.derive_base_ac_satisfied_from_verification_result(
            {"head_sha": SHA, "runtime_ac_results": [{"ac": "AC1", "status": "pass"}]}, live_main_sha=SHA
        )
        is None
    )

    # AC identity missing.
    missing_ac = _test_verdict([{"command_hash": "sha256:" + "1" * 64, "status": "pass", "exit_code": 0}])
    assert mod.derive_base_ac_satisfied_from_verification_result(missing_ac, live_main_sha=SHA) is None

    # command_hash missing.
    missing_hash = _test_verdict([{"ac": "AC1", "status": "pass", "exit_code": 0}])
    assert mod.derive_base_ac_satisfied_from_verification_result(missing_hash, live_main_sha=SHA) is None

    # exit_code != 0 despite a "pass" status must not be trusted as True.
    nonzero_exit = _test_verdict([_runtime_ac_entry("AC1", exit_code=1)])
    assert mod.derive_base_ac_satisfied_from_verification_result(nonzero_exit, live_main_sha=SHA) is False

    # fallback_detected / human_review_required / stop_condition_triggered
    # (per-entry) must each deny True.
    for flag in ("fallback_detected", "human_review_required", "stop_condition_triggered"):
        flagged = _test_verdict([_runtime_ac_entry("AC1", **{flag: True})])
        assert mod.derive_base_ac_satisfied_from_verification_result(flagged, live_main_sha=SHA) is False

    # Aggregate-level human_review_required (TEST_VERDICT top-level field,
    # not per-entry) must also deny True.
    aggregate_human_review = _test_verdict([_runtime_ac_entry("AC1")], human_review_required=True)
    assert (
        mod.derive_base_ac_satisfied_from_verification_result(aggregate_human_review, live_main_sha=SHA) is False
    )

    # SKIP / PARTIAL status entries must never be treated as pass.
    for status in ("skip", "partial", "SKIP", "PARTIAL"):
        skipped = _test_verdict([_runtime_ac_entry("AC1", status=status)])
        assert mod.derive_base_ac_satisfied_from_verification_result(skipped, live_main_sha=SHA) is False


# ---------------------------------------------------------------------------
# #2750: bounded carve-out for irrelevant non-closing cross-reference
# candidates whose own durable marker names a different Issue.
# ---------------------------------------------------------------------------


def test_2727_incident_shape_two_sibling_cross_references_do_not_conflict():
    """AC1/AC2: GIVEN the exact #2727 incident shape (two merged
    `verified_cross_reference` candidates, each a PR #2735/#2746 analogue
    whose own durable marker names a different sibling Issue with no other
    marker error) WHEN routed THEN neither is a qualified landing candidate
    for the current target Issue, `qualified_candidate_conflict` never
    fires, and the result falls through to `no_qualified_candidate` ->
    existing `already_satisfied` precedence (AC2)."""
    sibling_one = _candidate(
        lifecycle="merged",
        provenance="verified_cross_reference",
        pr_number=2735,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=2725),
    )
    sibling_two = _candidate(
        lifecycle="merged",
        provenance="verified_cross_reference",
        pr_number=2746,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=2726),
    )
    result = mod.derive_landing_disposition(
        _evidence(candidates=[sibling_one, sibling_two]), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert result["reason_codes"] == ["no_qualified_candidate"]

    composed = mod.apply_already_satisfied_precedence(
        result,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=True,
        route_loop_verdict_v2_module=route_mod,
    )
    assert composed["disposition"] == "already_satisfied"

    # No landing authority and base_ac_satisfied unproven -> canonical
    # ordinary_dispatch_or_explicit_recovery route, still never a conflict.
    unproven = mod.apply_already_satisfied_precedence(
        result,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=False,
        route_loop_verdict_v2_module=route_mod,
    )
    assert unproven["disposition"] == "ordinary_dispatch_or_explicit_recovery"


def test_valid_non_closing_candidate_survives_sibling_cross_reference_exclusion():
    """AC4: GIVEN a valid current-target non-closing candidate together with
    multiple irrelevant sibling candidates WHEN routed THEN the siblings are
    excluded from conflict counting and only the valid candidate is
    evaluated (no `qualified_candidate_conflict`)."""
    valid = _candidate(lifecycle="merged", provenance="verified_cross_reference", pr_number=100, ancestry=True)
    sibling_a = _candidate(
        lifecycle="merged",
        provenance="verified_cross_reference",
        pr_number=101,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=201),
    )
    sibling_b = _candidate(
        lifecycle="open",
        provenance="verified_cross_reference",
        pr_number=102,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=202),
    )
    evidence = _evidence(candidates=[valid, sibling_a, sibling_b], coverage=_exact_coverage())
    result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "implementation_already_landed"
    assert result["candidate"]["pr"]["number"] == 100


def test_non_closing_candidate_with_identity_mismatch_plus_other_marker_error_stays_fail_closed():
    """AC4: GIVEN a non-closing candidate whose marker error set is
    `scope_coverage_issue_identity_mismatch` PLUS another marker error
    (compound failure) WHEN routed alongside another qualified candidate
    THEN it is NOT treated as irrelevant -- it remains in conflict counting
    and the result stays `reconciliation_required` (fail-closed), never
    silently dropped."""
    compound_error_candidate = _candidate(
        lifecycle="merged",
        provenance="verified_cross_reference",
        pr_number=201,
        scope_coverage={
            "status": "invalid",
            "errors": ["scope_coverage_issue_identity_mismatch", "scope_coverage_manifest_digest_mismatch"],
        },
    )
    other = _candidate(lifecycle="open", provenance="verified_cross_reference", pr_number=202)
    result = mod.derive_landing_disposition(
        _evidence(candidates=[compound_error_candidate, other]), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "reconciliation_required"
    assert result["reason_codes"] == ["qualified_candidate_conflict"]

    # In isolation (no other qualified candidate), the compound-error
    # candidate's own marker invalidity is still fail-closed on its own.
    solo = mod.derive_landing_disposition(
        _evidence(candidates=[compound_error_candidate]), repo=REPO, issue_number=ISSUE
    )
    assert solo["disposition"] == "reconciliation_required"
    assert set(solo["reason_codes"]) == {
        "scope_coverage_issue_identity_mismatch",
        "scope_coverage_manifest_digest_mismatch",
    }


def test_closing_relation_identity_mismatch_stays_reconciliation_required():
    """AC3: GIVEN a `closing_relation` candidate (structured
    `closingIssuesReferences` names the current target Issue) whose own
    durable marker identity mismatches WHEN routed THEN the carve-out never
    applies (closing relation is a genuine contradiction, not an irrelevant
    cross-reference) and the result stays `reconciliation_required`."""
    closing_mismatch = _candidate(
        lifecycle="merged",
        provenance="closing_relation",
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=9999),
    )
    result = mod.derive_landing_disposition(_evidence(candidates=[closing_mismatch]), repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "reconciliation_required"
    assert result["reason_codes"] == ["scope_coverage_issue_identity_mismatch"]

    # Even alongside an otherwise-irrelevant sibling cross-reference, the
    # closing candidate's presence keeps the sibling-exclusion branch from
    # ever running (closing relation is selected first, per existing
    # closing-priority contract) -- the mismatch stays contradictory.
    sibling = _candidate(
        lifecycle="open",
        provenance="verified_cross_reference",
        pr_number=301,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=302),
    )
    result_with_sibling = mod.derive_landing_disposition(
        _evidence(candidates=[closing_mismatch, sibling]), repo=REPO, issue_number=ISSUE
    )
    assert result_with_sibling["disposition"] == "reconciliation_required"
    assert result_with_sibling["reason_codes"] == ["scope_coverage_issue_identity_mismatch"]


def test_markerless_2119_2137_candidate_is_never_excluded_by_sibling_mismatch_reasoning():
    """AC5: GIVEN a markerless (#2119/PR #2137-shaped) non-closing candidate
    WHEN evaluated against the #2750 carve-out THEN it is never treated as
    an irrelevant cross-reference (the carve-out only fires for a marker
    that parsed with the single `scope_coverage_issue_identity_mismatch`
    error, never for an absent marker) -- legacy markerless compatibility
    (#2699) is unaffected, including when it coexists with a genuinely
    irrelevant sibling."""
    markerless = _candidate(lifecycle="open", provenance="verified_cross_reference", pr_number=2137, ownership=True)
    assert mod._is_irrelevant_cross_reference(markerless) is False

    missing_marker_status = _candidate(
        lifecycle="open",
        provenance="verified_cross_reference",
        pr_number=2137,
        scope_coverage={"status": "missing_marker", "errors": ["scope_coverage_marker_missing"]},
        ownership=True,
    )
    assert mod._is_irrelevant_cross_reference(missing_marker_status) is False

    # Markerless legacy candidate resumes exactly as before (#2699) when it
    # is the sole qualified candidate after an irrelevant sibling (well-formed
    # marker naming a different Issue) is excluded.
    sibling = _candidate(
        lifecycle="draft",
        provenance="verified_cross_reference",
        pr_number=555,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=556),
    )
    result = mod.derive_landing_disposition(
        _evidence(candidates=[markerless, sibling]), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "existing_pr_resume"
    assert result["reason_codes"] == ["markerless_allowed_paths_coverage"]
    assert result["candidate"]["pr"]["number"] == 2137


def test_open_draft_lifecycle_gets_same_sibling_cross_reference_carve_out_as_merged():
    """AC1/AC5: the #2750 carve-out is not limited to `lifecycle == merged`
    -- an all-open/draft pair of irrelevant sibling `verified_cross_reference`
    candidates must be excluded exactly like the merged case, since
    `len(qualified) > 1` conflict counting runs before any lifecycle
    branch."""
    sibling_open = _candidate(
        lifecycle="open",
        provenance="verified_cross_reference",
        pr_number=601,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=701),
    )
    sibling_draft = _candidate(
        lifecycle="draft",
        provenance="verified_cross_reference",
        pr_number=602,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=702),
    )
    result = mod.derive_landing_disposition(
        _evidence(candidates=[sibling_open, sibling_draft]), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert result["reason_codes"] == ["no_qualified_candidate"]


def test_is_irrelevant_cross_reference_unit_boundaries():
    """Direct unit coverage of `_is_irrelevant_cross_reference()`'s exact
    boundary conditions: closing_relation is never eligible, a valid
    (non-invalid) marker is never eligible, and only the single-error
    identity-mismatch shape qualifies."""
    closing = _candidate(
        provenance="closing_relation", scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=1)
    )
    assert mod._is_irrelevant_cross_reference(closing) is False

    valid_marker = _candidate(provenance="verified_cross_reference", scope_coverage=_exact_coverage())
    assert mod._is_irrelevant_cross_reference(valid_marker) is False

    non_mapping_coverage = _candidate(provenance="verified_cross_reference", scope_coverage="not-a-mapping")
    assert mod._is_irrelevant_cross_reference(non_mapping_coverage) is False

    single_identity_mismatch = _candidate(
        provenance="verified_cross_reference", scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=1)
    )
    assert mod._is_irrelevant_cross_reference(single_identity_mismatch) is True


# ---------------------------------------------------------------------------
# research Issue #2761 (follow-up to PR #2758): once an irrelevant sibling
# candidate is freshly qualified (its own live body/closingIssuesReferences
# reduce to a lone `scope_coverage_issue_identity_mismatch`, no current-
# target closing relation), neither `collect_candidate_inputs()`'s
# materialization/ancestry-compare handling nor `_live_freshness_reference()`
# 's decision-time re-fetch may propagate that sibling's own transport
# failures or drift into the CURRENT target's `reconciliation_required`/
# `freshness_rebind_failed`.
# ---------------------------------------------------------------------------

_REAL_PR = 3000
_SIBLING_PR = 3100
_SIBLING_OTHER_ISSUE = 9001
_TARGET_ISSUE_BODY = "## Allowed Paths\n- `.claude/a.py`\n"


def _sibling_marker_body(
    *,
    other_issue_number=_SIBLING_OTHER_ISSUE,
    other_issue_body="## Allowed Paths\n- `.claude/other.py`\n",
    pr_head_sha=None,
):
    return mod.render_scope_coverage_marker(
        mod.build_scope_coverage_marker(
            issue_number=other_issue_number, issue_body=other_issue_body, pr_head_sha=pr_head_sha or ("f" * 40)
        )
    )


def _real_marker_body(*, target_issue_body=_TARGET_ISSUE_BODY, pr_head_sha):
    return mod.render_scope_coverage_marker(
        mod.build_scope_coverage_marker(issue_number=ISSUE, issue_body=target_issue_body, pr_head_sha=pr_head_sha)
    )


_LIFECYCLE_SHAPES = {
    "merged": {"state": "MERGED", "is_draft": False, "merged_at": "2026-01-02T00:00:00Z"},
    "open": {"state": "OPEN", "is_draft": False, "merged_at": None},
    "draft": {"state": "OPEN", "is_draft": True, "merged_at": None},
    "closed_unmerged": {"state": "CLOSED", "is_draft": False, "merged_at": None},
}


def _build_pipeline_run(
    *,
    include_real_candidate: bool = True,
    target_issue_body: str = _TARGET_ISSUE_BODY,
    real_head_sha: str = "1" * 40,
    sibling_lifecycle: str = "merged",
    sibling_collect_head_sha: str = "2" * 40,
    sibling_live_head_sha: str | None = None,
    sibling_collect_body: str | None = None,
    sibling_live_body: str | None = None,
    sibling_live_closing: bool = False,
    sibling_ancestry_call_should_fail: bool = True,
    sibling_light_fetch_fails: bool = False,
    sibling_live_body_field_override: Any = _UNSET,
    sibling_live_refs_field_override: Any = _UNSET,
    main_sha: str = "9" * 40,
):
    """Production-shaped `run_command` mock covering the FULL
    `collect_candidate_inputs()` -> `_live_freshness_reference()` call graph
    for one genuine current-target candidate (optional) plus one sibling
    `verified_cross_reference` candidate discovered only via the GitHub
    timeline (never `gh pr list`, matching the #2727 incident shape)."""
    sibling_collect_body = (
        sibling_collect_body
        if sibling_collect_body is not None
        else _sibling_marker_body(pr_head_sha=sibling_collect_head_sha)
    )
    sibling_live_body = sibling_live_body if sibling_live_body is not None else sibling_collect_body
    sibling_live_head_sha = sibling_live_head_sha or sibling_collect_head_sha

    real_body = _real_marker_body(target_issue_body=target_issue_body, pr_head_sha=real_head_sha)
    real_shape = _LIFECYCLE_SHAPES["merged"]
    real_merge_commit = {"oid": real_head_sha}

    sibling_shape = _LIFECYCLE_SHAPES[sibling_lifecycle]
    sibling_merge_commit_collect = {"oid": sibling_collect_head_sha} if sibling_lifecycle == "merged" else None
    sibling_merge_commit_live = {"oid": sibling_live_head_sha} if sibling_lifecycle == "merged" else None

    calls = {"ancestry_compare": 0}

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            rows = (
                [{"number": _REAL_PR, "closingIssuesReferences": [{"number": ISSUE}]}] if include_real_candidate else []
            )
            return 0, json.dumps(rows), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return (
                0,
                json.dumps(
                    [
                        {
                            "event": "cross-referenced",
                            "source": {
                                "issue": {
                                    "number": _SIBLING_PR,
                                    "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{_SIBLING_PR}"},
                                    "repository_url": f"https://api.github.com/repos/{REPO}",
                                }
                            },
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "compare" in argv[2]:
            # Only the REAL candidate's ancestry compare may legitimately be
            # invoked (skip-by-construction must prevent the sibling's own
            # compare call from ever happening at all). If the sibling's
            # merge_oid shows up here, this is exactly the bug under test --
            # fail loudly rather than silently answering it.
            if sibling_collect_head_sha in argv[2]:
                calls["ancestry_compare"] += 1
                if sibling_ancestry_call_should_fail:
                    return 1, "", "simulated ancestry compare transport failure"
                return 0, "ahead\n", ""
            return 0, "ahead\n", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        if argv[:3] == ["gh", "pr", "view"]:
            number = argv[3]
            if argv[-1] == mod._LIVE_CANDIDATE_REFRESH_FIELDS:
                if number == str(_REAL_PR):
                    return (
                        0,
                        json.dumps(
                            {
                                "headRefOid": real_head_sha,
                                "mergedAt": real_shape["merged_at"],
                                "mergeCommit": real_merge_commit,
                                "body": real_body,
                                "closingIssuesReferences": [{"number": ISSUE}],
                            }
                        ),
                        "",
                    )
                if number == str(_SIBLING_PR):
                    if sibling_light_fetch_fails:
                        return 1, "", "simulated sibling live fetch transport failure"
                    refs = [{"number": ISSUE}] if sibling_live_closing else [{"number": _SIBLING_OTHER_ISSUE}]
                    if sibling_live_refs_field_override is not _UNSET:
                        refs = sibling_live_refs_field_override
                    body_value = sibling_live_body
                    if sibling_live_body_field_override is not _UNSET:
                        body_value = sibling_live_body_field_override
                    live_payload = {
                        "headRefOid": sibling_live_head_sha,
                        "mergedAt": sibling_shape["merged_at"],
                        "mergeCommit": sibling_merge_commit_live,
                        "body": body_value,
                        "closingIssuesReferences": refs,
                    }
                    if live_payload["body"] is _MISSING_FIELD:
                        del live_payload["body"]
                    if live_payload["closingIssuesReferences"] is _MISSING_FIELD:
                        del live_payload["closingIssuesReferences"]
                    return (0, json.dumps(live_payload), "")
                return 1, "", f"unexpected light-field pr view for {number}"
            if number == str(_REAL_PR):
                return (
                    0,
                    json.dumps(
                        {
                            "number": _REAL_PR,
                            "url": f"https://github.com/{REPO}/pull/{_REAL_PR}",
                            "state": real_shape["state"],
                            "isDraft": real_shape["is_draft"],
                            "mergedAt": real_shape["merged_at"],
                            "mergeCommit": real_merge_commit,
                            "headRefOid": real_head_sha,
                            "closingIssuesReferences": [{"number": ISSUE}],
                            "body": real_body,
                            "files": [],
                        }
                    ),
                    "",
                )
            if number == str(_SIBLING_PR):
                return (
                    0,
                    json.dumps(
                        {
                            "number": _SIBLING_PR,
                            "url": f"https://github.com/{REPO}/pull/{_SIBLING_PR}",
                            "state": sibling_shape["state"],
                            "isDraft": sibling_shape["is_draft"],
                            "mergedAt": sibling_shape["merged_at"],
                            "mergeCommit": sibling_merge_commit_collect,
                            "headRefOid": sibling_collect_head_sha,
                            "closingIssuesReferences": [],
                            "body": sibling_collect_body,
                            "files": [],
                        }
                    ),
                    "",
                )
            return 1, "", f"unexpected pr view for {number}"
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"body": target_issue_body}), ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    return run, calls


def test_freshness_rebind_irrelevant_sibling_across_all_lifecycles_does_not_block_target():
    """AC1/AC2 (scenarios 1-4): GIVEN a genuine merged closing-relation
    candidate for the CURRENT target Issue, together with a qualified-
    irrelevant `verified_cross_reference` sibling candidate whose own marker
    names a different Issue, in each of the open/draft/merged/closed_unmerged
    lifecycle shapes, WHEN `resolve_landing_disposition_with_freshness_
    rebind()` runs THEN the sibling never causes `reconciliation_required`/
    `freshness_rebind_failed` for the current target -- the genuine
    candidate's `implementation_already_landed` disposition is reached in
    every case, and (for the merged sibling) the main-ancestry compare
    transport call is never even attempted (skip-by-construction)."""
    for lifecycle in ("open", "draft", "merged", "closed_unmerged"):
        run, calls = _build_pipeline_run(sibling_lifecycle=lifecycle, sibling_ancestry_call_should_fail=True)
        result = mod.resolve_landing_disposition_with_freshness_rebind(
            repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
        )
        assert result["decision_time_rebind"]["status"] == "fresh", lifecycle
        assert result["landing_disposition"]["disposition"] == "implementation_already_landed", lifecycle
        assert result["landing_disposition"]["reason_codes"] != ["qualified_candidate_conflict"], lifecycle
        if lifecycle == "merged":
            assert calls["ancestry_compare"] == 0, "merged sibling ancestry compare must be skipped by construction"


def test_collect_candidate_inputs_skips_ancestry_compare_for_qualified_irrelevant_merged_sibling():
    """AC2 (scenario 5): GIVEN a merged `verified_cross_reference` candidate
    already qualified as an irrelevant sibling (own marker names a different
    Issue) WHEN `collect_candidate_inputs()` runs THEN the merged main-
    ancestry compare transport call is never invoked for it (skip-by-
    construction) and `_candidate_errors()`/`validate_implementation_landed_
    evidence()` never raises `merged_candidate_main_ancestry_unverified` for
    it, so a would-be ancestry-compare transport failure never reaches the
    current target's disposition."""
    sibling_body = _sibling_marker_body(pr_head_sha="2" * 40)
    rows = [{"number": _REAL_PR, "closingIssuesReferences": [{"number": ISSUE}]}]
    timeline = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": _SIBLING_PR,
                    "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{_SIBLING_PR}"},
                    "repository_url": f"https://api.github.com/repos/{REPO}",
                }
            },
        }
    ]
    real_body = _real_marker_body(pr_head_sha="1" * 40)
    calls = {"ancestry_compare": 0}

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(rows), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, json.dumps(timeline), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "compare" in argv[2]:
            # Only the sibling's own ancestry compare (identified by its
            # merge_oid) must never happen (skip-by-construction). The
            # REAL candidate's own ancestry compare is a separate,
            # legitimate call that must still succeed.
            if ("2" * 40) in argv[2]:
                calls["ancestry_compare"] += 1
                return 1, "", "simulated ancestry compare transport failure"
            return 0, "ahead\n", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, "9" * 40 + "\n", ""
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == str(_REAL_PR):
            return (
                0,
                json.dumps(
                    {
                        "number": _REAL_PR,
                        "url": f"https://github.com/{REPO}/pull/{_REAL_PR}",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-01-01T00:00:00Z",
                        "mergeCommit": {"oid": "1" * 40},
                        "headRefOid": "1" * 40,
                        "closingIssuesReferences": [{"number": ISSUE}],
                        "body": real_body,
                        "files": [],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == str(_SIBLING_PR):
            return (
                0,
                json.dumps(
                    {
                        "number": _SIBLING_PR,
                        "url": f"https://github.com/{REPO}/pull/{_SIBLING_PR}",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-01-02T00:00:00Z",
                        "mergeCommit": {"oid": "2" * 40},
                        "headRefOid": "2" * 40,
                        "closingIssuesReferences": [],
                        "body": sibling_body,
                        "files": [],
                    }
                ),
                "",
            )
        return 1, "", "unexpected argv: " + " ".join(argv)

    evidence = mod.collect_candidate_inputs(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    assert calls["ancestry_compare"] == 0
    sibling_candidate = next(c for c in evidence["candidates"] if c["pr"]["number"] == _SIBLING_PR)
    assert sibling_candidate["qualified_irrelevant_sibling"] is True
    assert sibling_candidate["main_ancestry"] == {"verified": False, "reachable": False}
    validated = mod.validate_implementation_landed_evidence(evidence, repo=REPO, issue_number=ISSUE)
    assert validated["valid"] is True, validated["errors"]
    result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "implementation_already_landed"


def test_collect_candidate_inputs_sibling_live_body_fetch_failure_stays_fail_closed():
    """AC3 (scenario 6): GIVEN a discovered sibling candidate whose OWN `gh
    pr view` detail/body fetch fails during `collect_candidate_inputs()`
    (qualification itself cannot be attempted without a live body) WHEN
    evidence is validated THEN it stays fail-closed
    (`materialization_failures` records it, `contradictory` is True, and
    `derive_landing_disposition()` returns `reconciliation_required`) exactly
    as before this fix -- this failure mode is untouched by the #2750/#2761
    carve-out, which only ever applies to a candidate whose live body WAS
    successfully fetched."""
    rows = [{"number": _REAL_PR, "closingIssuesReferences": [{"number": ISSUE}]}]
    timeline = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": _SIBLING_PR,
                    "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{_SIBLING_PR}"},
                    "repository_url": f"https://api.github.com/repos/{REPO}",
                }
            },
        }
    ]
    real_body = _real_marker_body(pr_head_sha="1" * 40)

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(rows), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, json.dumps(timeline), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, "9" * 40 + "\n", ""
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == str(_REAL_PR):
            return (
                0,
                json.dumps(
                    {
                        "number": _REAL_PR,
                        "url": f"https://github.com/{REPO}/pull/{_REAL_PR}",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-01-01T00:00:00Z",
                        "mergeCommit": {"oid": "1" * 40},
                        "headRefOid": "1" * 40,
                        "closingIssuesReferences": [{"number": ISSUE}],
                        "body": real_body,
                        "files": [],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == str(_SIBLING_PR):
            return 1, "", "simulated sibling detail fetch transport failure"
        return 1, "", "unexpected argv: " + " ".join(argv)

    evidence = mod.collect_candidate_inputs(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    assert evidence["materialization_failures"] == [_SIBLING_PR]
    assert evidence["contradictory"] is True
    result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "reconciliation_required"
    assert "evidence_contradictory" in result["reason_codes"]


def test_freshness_rebind_reconfirms_irrelevant_sibling_from_changed_decision_time_body():
    """AC1 (scenario 7): GIVEN a sibling candidate whose PR body differs
    between collection time and decision time (different wording / a
    different named sibling Issue number) BUT both independently re-derive
    to a lone `scope_coverage_issue_identity_mismatch` WHEN `resolve_landing_
    disposition_with_freshness_rebind()` runs THEN qualification succeeds
    from the FRESH decision-time re-parse -- never requiring byte-equality
    to the collection-time body -- and the current target's genuine
    candidate still reaches `implementation_already_landed`."""
    collect_body = _sibling_marker_body(other_issue_number=9001, pr_head_sha="2" * 40)
    live_body = _sibling_marker_body(other_issue_number=9002, pr_head_sha="2" * 40)
    assert collect_body != live_body

    run, _calls = _build_pipeline_run(
        sibling_lifecycle="merged",
        sibling_collect_body=collect_body,
        sibling_live_body=live_body,
    )
    result = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    assert result["decision_time_rebind"]["status"] == "fresh"
    assert result["landing_disposition"]["disposition"] == "implementation_already_landed"


def test_freshness_rebind_sibling_now_showing_current_target_authority_is_not_excluded():
    """AC1 (scenario 8): GIVEN a sole sibling candidate that was collection-
    time-qualified irrelevant, but whose LIVE decision-time
    `closingIssuesReferences` now names the CURRENT target Issue WHEN
    `resolve_landing_disposition_with_freshness_rebind()` runs THEN the
    #2750/#2761 carve-out must NOT apply -- the sibling is promoted back to
    normal `closing_relation` handling (existing target-authority handling
    proceeds normally) and, because its own marker still names a different
    Issue, the EXISTING fail-closed closing-relation-with-mismatched-marker
    behavior fires (`reconciliation_required`), never a silently-excluded
    `no_qualified_candidate`."""
    run, _calls = _build_pipeline_run(
        include_real_candidate=False,
        sibling_lifecycle="open",
        sibling_live_closing=True,
    )
    result = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    assert result["decision_time_rebind"]["status"] == "fresh"
    assert result["landing_disposition"]["disposition"] == "reconciliation_required"
    assert result["landing_disposition"]["reason_codes"] == ["scope_coverage_issue_identity_mismatch"]
    assert result["landing_disposition"]["candidate"]["pr"]["number"] == _SIBLING_PR
    assert result["landing_disposition"]["candidate"]["provenance"]["kind"] == "closing_relation"


def test_freshness_rebind_markerless_legacy_sibling_never_enters_carve_out_path():
    """AC3/scenario 9: GIVEN a markerless (legacy, #2119/PR #2137-shaped)
    `verified_cross_reference` sibling candidate coexisting with a genuine
    current-target candidate WHEN `resolve_landing_disposition_with_
    freshness_rebind()` runs THEN the markerless candidate is never
    misclassified as `qualified_irrelevant_sibling` (the carve-out only
    fires for a marker that parsed with the single identity-mismatch error,
    never for an absent marker) and legacy markerless compatibility is
    unaffected."""
    run, _calls = _build_pipeline_run(sibling_lifecycle="open", sibling_collect_body="", sibling_live_body="")
    result = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    sibling_candidate = next(c for c in result["candidates"] if c["pr"]["number"] == _SIBLING_PR)
    assert sibling_candidate["qualified_irrelevant_sibling"] is False
    assert result["decision_time_rebind"]["status"] == "fresh"
    assert result["landing_disposition"]["disposition"] == "implementation_already_landed"


def test_collect_candidate_inputs_compound_marker_error_sibling_is_not_qualified_irrelevant():
    """AC3/scenario 10: GIVEN a sibling `verified_cross_reference` candidate
    whose own marker error set is identity-mismatch PLUS another marker
    error (compound failure, e.g. a corrupted `scope_manifest` digest) WHEN
    `collect_candidate_inputs()` runs THEN it is NOT flagged `qualified_
    irrelevant_sibling` (stays fail-closed / in conflict counting), and its
    merged main-ancestry compare is NOT skipped (the skip-by-construction
    carve-out never applies to it)."""
    marker = mod.build_scope_coverage_marker(
        issue_number=_SIBLING_OTHER_ISSUE, issue_body="## Allowed Paths\n- `.claude/other.py`\n", pr_head_sha="2" * 40
    )
    marker[mod.COVERAGE_SCHEMA]["normalized_scope_manifest_sha256"] = "sha256:" + "0" * 64  # corrupt digest
    corrupted_body = mod.render_scope_coverage_marker(marker)

    rows: list[dict] = []
    timeline = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": _SIBLING_PR,
                    "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{_SIBLING_PR}"},
                    "repository_url": f"https://api.github.com/repos/{REPO}",
                }
            },
        }
    ]
    calls = {"ancestry_compare": 0}

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(rows), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, json.dumps(timeline), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "compare" in argv[2]:
            calls["ancestry_compare"] += 1
            return 0, "ahead\n", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, "9" * 40 + "\n", ""
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == str(_SIBLING_PR):
            return (
                0,
                json.dumps(
                    {
                        "number": _SIBLING_PR,
                        "url": f"https://github.com/{REPO}/pull/{_SIBLING_PR}",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-01-02T00:00:00Z",
                        "mergeCommit": {"oid": "2" * 40},
                        "headRefOid": "2" * 40,
                        "closingIssuesReferences": [],
                        "body": corrupted_body,
                        "files": [],
                    }
                ),
                "",
            )
        return 1, "", "unexpected argv: " + " ".join(argv)

    evidence = mod.collect_candidate_inputs(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    sibling_candidate = evidence["candidates"][0]
    assert sibling_candidate["pr"]["number"] == _SIBLING_PR
    assert sibling_candidate["qualified_irrelevant_sibling"] is False
    assert set(sibling_candidate["scope_coverage"]["errors"]) == {
        "scope_coverage_issue_identity_mismatch",
        "scope_coverage_manifest_digest_mismatch",
    }
    assert calls["ancestry_compare"] == 1
    result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "reconciliation_required"


def test_freshness_rebind_open_draft_sibling_head_drift_does_not_fail_target_freshness():
    """AC2/AC6 (scenario 11, extended by research Issue #2761 P2-3): GIVEN
    an open/draft/closed-unmerged `verified_cross_reference` sibling
    candidate already qualified irrelevant at collection time, whose live
    `headRefOid` DRIFTS between collection and decision time (still freshly
    reconfirmed irrelevant otherwise, from FRESH decision-time body +
    closing-relation authority) WHEN `resolve_landing_disposition_with_
    freshness_rebind()` runs THEN this head drift does not cause
    `freshness_rebind_failed`/`reconciliation_required` for the current
    target -- the genuine current-target candidate still reaches
    `implementation_already_landed` -- including for a CLOSED-UNMERGED
    sibling, not just open/draft."""
    for lifecycle in ("open", "draft", "closed_unmerged"):
        run, _calls = _build_pipeline_run(
            sibling_lifecycle=lifecycle,
            sibling_collect_head_sha="2" * 40,
            sibling_live_head_sha="3" * 40,
        )
        result = mod.resolve_landing_disposition_with_freshness_rebind(
            repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
        )
        assert result["decision_time_rebind"]["status"] == "fresh", lifecycle
        assert result["landing_disposition"]["disposition"] == "implementation_already_landed", lifecycle


def test_freshness_rebind_qualified_sibling_decision_time_fetch_failure_is_unknown_not_confirmed_irrelevant():
    """research Issue #2761 P1-1/P2-1 (blocker regression): GIVEN a
    collection-time-qualified-irrelevant `verified_cross_reference` sibling
    candidate whose decision-time `gh pr view` (`_LIVE_CANDIDATE_REFRESH_
    FIELDS`) transport call FAILS WHEN `resolve_landing_disposition_with_
    freshness_rebind()` runs THEN the candidate is treated as UNKNOWN, never
    as "still confirmed irrelevant" -- fresh authority (`body`/
    `closingIssuesReferences`) could not be re-derived, so it is NOT
    excluded from the freshness identity requirement, and (after the
    existing bounded retry, which also cannot reconfirm it because the same
    transport failure persists) the overall result is `reconciliation_
    required` / `freshness_rebind_failed` -- exactly the same fail-closed
    outcome as any other decision-time transport failure, never a silent
    fail-open pass-through for the current target."""
    run, _calls = _build_pipeline_run(sibling_lifecycle="merged", sibling_light_fetch_fails=True)
    result = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    assert result["decision_time_rebind"]["status"] == "stale"
    assert result["landing_disposition"]["disposition"] == "reconciliation_required"
    assert result["landing_disposition"]["reason_codes"] == ["freshness_rebind_failed"]


def test_freshness_rebind_qualified_sibling_unverified_closing_authority_is_not_confirmed_irrelevant():
    """research Issue #2761 P1-2/P2-2 (blocker regression, parameterized):
    GIVEN a collection-time-qualified-irrelevant `verified_cross_reference`
    sibling candidate whose decision-time `gh pr view` response has a
    `body` or `closingIssuesReferences` field that is MISSING / `None` /
    the wrong type WHEN `resolve_landing_disposition_with_freshness_
    rebind()` runs THEN that field-level ambiguity is never treated as
    "verified: no closing relation" or "verified: empty body" -- the
    candidate's fresh negative qualification cannot be re-derived, so it
    stays UNKNOWN (not excluded from the freshness identity requirement),
    and the bounded-retried result is `reconciliation_required` /
    `freshness_rebind_failed` in every boundary case."""
    boundary_cases = (
        ("body", _MISSING_FIELD),
        ("body", None),
        ("body", 12345),
        ("closingIssuesReferences", _MISSING_FIELD),
        ("closingIssuesReferences", None),
        ("closingIssuesReferences", "not-a-list"),
    )
    for field, bad_value in boundary_cases:
        overrides = {
            "sibling_live_body_field_override": bad_value if field == "body" else _UNSET,
            "sibling_live_refs_field_override": bad_value if field == "closingIssuesReferences" else _UNSET,
        }
        run, _calls = _build_pipeline_run(sibling_lifecycle="merged", **overrides)
        result = mod.resolve_landing_disposition_with_freshness_rebind(
            repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
        )
        label = f"{field}={bad_value!r}"
        assert result["decision_time_rebind"]["status"] == "stale", label
        assert result["landing_disposition"]["disposition"] == "reconciliation_required", label
        assert result["landing_disposition"]["reason_codes"] == ["freshness_rebind_failed"], label


def test_freshness_rebind_qualified_sibling_verified_empty_closing_refs_still_confirms_irrelevant():
    """research Issue #2761 P1-2 (positive control): GIVEN a collection-
    time-qualified-irrelevant sibling candidate whose decision-time
    `closingIssuesReferences` is a VERIFIED (successfully fetched, correctly
    typed) empty list `[]` WHEN `resolve_landing_disposition_with_
    freshness_rebind()` runs THEN this is treated as "confirmed: no closing
    relation" (not as unconfirmed authority) -- the sibling's fresh negative
    qualification succeeds normally and the genuine current-target candidate
    still reaches `implementation_already_landed`, proving `[]` and
    missing/None/invalid-type are NOT conflated."""
    run, _calls = _build_pipeline_run(
        sibling_lifecycle="merged",
        sibling_live_refs_field_override=[],
    )
    result = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=_TARGET_ISSUE_BODY, run_command=run
    )
    assert result["decision_time_rebind"]["status"] == "fresh"
    assert result["landing_disposition"]["disposition"] == "implementation_already_landed"


def test_parse_marker_unparsable_marker_fence_is_invalid_not_missing():
    """Issue #2811 P1 (Blocker B): a YAML fence that names the marker schema but cannot be
    parsed is a malformed marker candidate (invalid), never `scope_coverage_marker_missing`."""
    issue_number = 2811
    invalid = ["scope_coverage_marker_ambiguous_or_invalid"]
    missing = ["scope_coverage_marker_missing"]
    unparsable_marker = "```yaml\nIMPLEMENTATION_SCOPE_COVERAGE_V1:\n  schema_version: [\n```\n"
    unparsable_other = "```yaml\nunrelated_key: [\n```\n"
    prose_only = "IMPLEMENTATION_SCOPE_COVERAGE_V1: は生成済みです。\n"
    valid_yaml_non_marker = "```yaml\nnote: IMPLEMENTATION_SCOPE_COVERAGE_V1 is generated later\n```\n"
    marker = mod.build_scope_coverage_marker(
        issue_number=issue_number, issue_body=_scope_manifest_issue_body(), pr_head_sha="a" * 40
    )
    valid_block = mod.render_scope_coverage_marker(marker)

    # parse-unparsable fence that names the schema: invalid (not missing).
    result, errors = mod._parse_marker("## Summary\n\n" + unparsable_marker, issue_number=issue_number)
    assert result is None
    assert errors == invalid

    # Unparsable fence WITHOUT the schema token: unchanged behavior (ignored -> missing).
    result, errors = mod._parse_marker("## Summary\n\n" + unparsable_other, issue_number=issue_number)
    assert result is None
    assert errors == missing

    # Prose-only token (no fence) and a valid YAML fence that merely mentions the token: missing.
    missing_bodies = (
        prose_only,
        "## Summary\n\n" + valid_yaml_non_marker,
        "## Summary\n\n" + prose_only + unparsable_other,
    )
    for body in missing_bodies:
        result, errors = mod._parse_marker(body, issue_number=issue_number)
        assert result is None, body
        assert errors == missing, (body, errors)

    # Valid marker alone: returned.
    result, errors = mod._parse_marker("## Summary\n\n" + valid_block + "\n", issue_number=issue_number)
    assert errors == []
    assert result is not None and result["issue_number"] == issue_number

    # Valid marker + another unparsable marker candidate: fail-closed (either order).
    for body in (valid_block + "\n\n" + unparsable_marker, unparsable_marker + "\n\n" + valid_block + "\n"):
        result, errors = mod._parse_marker(body, issue_number=issue_number)
        assert result is None
        assert errors == invalid


# ---------------------------------------------------------------------------
# #2893: historical merged later_scope_expansion carve-out. A merged,
# main-reachable `verified_cross_reference` candidate whose own valid
# same-Issue marker records `later_scope_expansion` stops counting as a
# conflict against a unique fresh exact open/draft candidate -- and nothing
# else changes.
# ---------------------------------------------------------------------------

_HISTORICAL_MERGED_BODY = "## Allowed Paths\n- `.claude/a.py`\n"
_EXPANDED_LIVE_BODY = "## Allowed Paths\n- `.claude/a.py`\n- `.claude/b.py`\n"


def _real_marker_coverage(*, merged_body: str, live_body: str, marker_issue: int = ISSUE):
    """Candidate-local coverage produced by the production marker path
    (`build_scope_coverage_marker()` / `render_scope_coverage_marker()` ->
    `coverage_from_pr_body()`), never a hand-built status dict."""
    marker = mod.render_scope_coverage_marker(
        mod.build_scope_coverage_marker(issue_number=marker_issue, issue_body=merged_body, pr_head_sha=SHA)
    )
    return mod.coverage_from_pr_body(pr_body=marker, issue_number=ISSUE, live_issue_body=live_body)


def _historical_coverage():
    return _real_marker_coverage(merged_body=_HISTORICAL_MERGED_BODY, live_body=_EXPANDED_LIVE_BODY)


def _current_exact_coverage():
    return _real_marker_coverage(merged_body=_EXPANDED_LIVE_BODY, live_body=_EXPANDED_LIVE_BODY)


def _historical_merged(**overrides):
    kwargs = {
        "lifecycle": "merged",
        "provenance": "verified_cross_reference",
        "pr_number": 2851,
        "scope_coverage": _historical_coverage(),
    }
    kwargs.update(overrides)
    return _candidate(**kwargs)


def _current_exact(**overrides):
    kwargs = {
        "lifecycle": "draft",
        "provenance": "verified_cross_reference",
        "pr_number": 2888,
        "ownership": False,
        "scope_coverage": _current_exact_coverage(),
    }
    kwargs.update(overrides)
    return _candidate(**kwargs)


def _disposition(candidates, **evidence_kwargs):
    return mod.derive_landing_disposition(
        _evidence(candidates=candidates, **evidence_kwargs), repo=REPO, issue_number=ISSUE
    )


def test_ac1_historical_merged_later_scope_expansion_with_unique_current_exact_resumes():
    """GIVEN the #2851/#2888 shape (no closing relation; merged
    `later_scope_expansion` + one fresh exact draft) WHEN routed THEN the
    draft is the `existing_pr_resume` authority, not a false conflict."""
    assert _historical_coverage()["status"] == "later_scope_expansion"
    assert _current_exact_coverage()["status"] == "covered_exactly"
    for order in ((_historical_merged(), _current_exact()), (_current_exact(), _historical_merged())):
        result = _disposition(list(order))
        assert result["disposition"] == "existing_pr_resume"
        assert result["reason_codes"] == []
        assert result["candidate"]["pr"]["number"] == 2888
    # `open` current candidates are equivalent to `draft`.
    open_result = _disposition([_historical_merged(), _current_exact(lifecycle="open")])
    assert open_result["disposition"] == "existing_pr_resume"
    assert open_result["candidate"]["lifecycle"] == "open"


def test_ac2_historical_candidate_is_retained_in_evidence():
    """GIVEN the same shape WHEN routed THEN the historical candidate stays in
    `evidence["candidates"]` untouched (only conflict counting changes)."""
    evidence = _evidence(candidates=[_historical_merged(), _current_exact()])
    before = json.dumps(evidence, sort_keys=True)
    result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "existing_pr_resume"
    assert result["candidate"]["pr"]["number"] == 2888
    assert json.dumps(evidence, sort_keys=True) == before
    assert [c["pr"]["number"] for c in evidence["candidates"]] == [2851, 2888]
    retained = evidence["candidates"][0]
    assert retained["lifecycle"] == "merged"
    assert retained["scope_coverage"]["status"] == "later_scope_expansion"
    assert "qualified_candidate_conflict" not in result["reason_codes"]


def test_ac3_two_current_exact_candidates_keep_conflict():
    """GIVEN a historical merged candidate and two current exact open/draft
    candidates WHEN routed THEN the conflict is preserved."""
    for second in ({"lifecycle": "draft"}, {"lifecycle": "open"}):
        result = _disposition(
            [_historical_merged(), _current_exact(pr_number=2888), _current_exact(pr_number=2890, **second)]
        )
        assert result["disposition"] == "reconciliation_required"
        assert result["reason_codes"] == ["qualified_candidate_conflict"]
        assert result["candidate"] is None
    # Without the historical candidate the two current candidates conflict too.
    bare = _disposition([_current_exact(pr_number=2888), _current_exact(pr_number=2890)])
    assert bare["reason_codes"] == ["qualified_candidate_conflict"]


def _ac4_merged_exact_current_plus_draft():
    exact_merged = _historical_merged(scope_coverage=_current_exact_coverage())
    return _evidence(candidates=[exact_merged, _current_exact()]), "qualified_candidate_conflict"


def _ac4_malformed_marker():
    malformed = {"status": "invalid", "errors": ["scope_coverage_manifest_digest_mismatch"]}
    return (
        _evidence(candidates=[_historical_merged(scope_coverage=malformed), _current_exact()]),
        "qualified_candidate_conflict",
    )


def _ac4_marker_attached_with_other_issue_identity():
    other_issue = _historical_coverage()
    other_issue["marker"] = dict(other_issue["marker"], issue_number=ISSUE + 1)
    return (
        _evidence(candidates=[_historical_merged(scope_coverage=other_issue), _current_exact()]),
        "qualified_candidate_conflict",
    )


def _ac4_candidate_target_identity_mismatch():
    merged = _historical_merged()
    merged["target"] = {"repo": REPO, "issue_number": ISSUE + 1}
    return _evidence(candidates=[merged, _current_exact()]), "candidate[0]:candidate_target_identity_mismatch"


def _ac4_stale_evidence():
    return (
        _evidence(candidates=[_historical_merged(), _current_exact()], freshness="stale"),
        "evidence_stale_or_missing_freshness",
    )


def _ac4_stale_current_head():
    return (
        _evidence(candidates=[_historical_merged(), _current_exact(fresh=False)]),
        "qualified_candidate_conflict",
    )


def _ac4_not_main_reachable():
    return (
        _evidence(candidates=[_historical_merged(ancestry=False), _current_exact()]),
        "candidate[0]:merged_candidate_main_ancestry_unverified",
    )


def _ac4_verified_but_unreachable_from_main():
    merged = _historical_merged()
    merged["main_ancestry"] = {"verified": True, "reachable": False}
    return _evidence(candidates=[merged, _current_exact()]), "qualified_candidate_conflict"


def _ac4_markerless_merged():
    markerless = {"status": "missing_marker", "errors": ["scope_coverage_marker_missing"]}
    return (
        _evidence(candidates=[_historical_merged(scope_coverage=markerless), _current_exact()]),
        "qualified_candidate_conflict",
    )


def _ac4_current_candidate_not_exact():
    return (
        _evidence(candidates=[_historical_merged(), _current_exact(scope_coverage=_historical_coverage())]),
        "qualified_candidate_conflict",
    )


def _ac4_current_candidate_other_issue_marker():
    other_issue = _current_exact_coverage()
    other_issue["marker"] = dict(other_issue["marker"], issue_number=ISSUE + 1)
    return (
        _evidence(candidates=[_historical_merged(), _current_exact(scope_coverage=other_issue)]),
        "qualified_candidate_conflict",
    )


def _ac4_extra_closed_unmerged_candidate():
    return (
        _evidence(
            candidates=[
                _historical_merged(),
                _current_exact(),
                _candidate(lifecycle="closed_unmerged", provenance="verified_cross_reference", pr_number=2700),
            ]
        ),
        "qualified_candidate_conflict",
    )


def _ac4_contradictory_evidence():
    return (
        _evidence(candidates=[_historical_merged(), _current_exact()], contradictory=True),
        "evidence_contradictory",
    )


@pytest.mark.parametrize(
    "build_case",
    [
        _ac4_merged_exact_current_plus_draft,
        _ac4_malformed_marker,
        _ac4_marker_attached_with_other_issue_identity,
        _ac4_candidate_target_identity_mismatch,
        _ac4_stale_evidence,
        _ac4_stale_current_head,
        _ac4_not_main_reachable,
        _ac4_verified_but_unreachable_from_main,
        _ac4_markerless_merged,
        _ac4_current_candidate_not_exact,
        _ac4_current_candidate_other_issue_marker,
        _ac4_extra_closed_unmerged_candidate,
        _ac4_contradictory_evidence,
    ],
)
def test_ac4_non_historical_merged_states_keep_fail_closed(build_case):
    """GIVEN a merged candidate that is exact-current / malformed / identity
    mismatched / stale / not main-reachable / markerless (or an unsafe current
    candidate) WHEN routed THEN the carve-out does not apply and the existing
    fail-closed semantics hold."""
    evidence, expected_reason = build_case()
    result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "reconciliation_required"
    assert expected_reason in result["reason_codes"]
    assert result["disposition"] != "existing_pr_resume"


def test_ac5_closing_relation_precedence_is_unchanged():
    """GIVEN a `closing_relation` candidate WHEN routed THEN the structured
    closing authority keeps its existing precedence and the new carve-out is
    never applied (not even to a historical-shaped cross-reference)."""
    # Closing merged candidate recording later_scope_expansion wins over the
    # cross-reference draft: legacy/later-scope -> ordinary dispatch. If the
    # carve-out leaked into the closing branch this would become a resume.
    closing_merged = _historical_merged(provenance="closing_relation", pr_number=2851)
    result = _disposition([closing_merged, _current_exact()])
    assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert result["reason_codes"] == ["legacy_or_later_scope_expansion"]
    assert result["candidate"]["pr"]["number"] == 2851

    # A closing open candidate alongside the historical cross-reference is the
    # sole authority (existing precedence, historical candidate not consulted).
    closing_open = _current_exact(provenance="closing_relation", pr_number=2900)
    assert _disposition([_historical_merged(), closing_open])["candidate"]["pr"]["number"] == 2900

    # Two closing candidates keep the same-authority conflict even when one is
    # historical-shaped.
    closing_historical = _historical_merged(provenance="closing_relation")
    conflict = _disposition([closing_historical, _current_exact(provenance="closing_relation", pr_number=2900)])
    assert conflict["disposition"] == "reconciliation_required"
    assert conflict["reason_codes"] == ["qualified_candidate_conflict"]


def test_ac6_existing_carveouts_and_legacy_behavior_are_unchanged():
    """GIVEN pre-#2893 shapes WHEN routed THEN dispositions are unchanged:
    #2750 sibling carve-out, markerless legacy, closed-unmerged,
    implementation_already_landed and already_satisfied precedence."""
    # #2750: irrelevant siblings are excluded, a valid draft still resumes.
    sibling = _candidate(
        lifecycle="merged",
        provenance="verified_cross_reference",
        pr_number=2735,
        scope_coverage=_sibling_identity_mismatch_coverage(other_issue_number=2725),
    )
    sibling_only = _disposition([sibling])
    assert sibling_only["reason_codes"] == ["no_qualified_candidate"]
    sibling_plus_draft = _disposition([sibling, _current_exact()])
    assert sibling_plus_draft["disposition"] == "existing_pr_resume"
    assert sibling_plus_draft["candidate"]["pr"]["number"] == 2888

    # Markerless legacy merged: ordinary dispatch; with an extra open
    # candidate the pre-existing conflict is retained (not a historical carve-out).
    legacy = _candidate(lifecycle="merged", provenance="verified_cross_reference", pr_number=2137)
    legacy_result = _disposition(
        [legacy], coverage=mod.coverage_from_pr_body(pr_body="", issue_number=ISSUE, live_issue_body="x")
    )
    assert legacy_result["reason_codes"] == ["legacy_or_later_scope_expansion"]
    legacy_conflict = _disposition(
        [legacy, _candidate(lifecycle="open", provenance="verified_cross_reference", pr_number=2888)]
    )
    assert legacy_conflict["reason_codes"] == ["qualified_candidate_conflict"]

    # Closed-unmerged alone stays ordinary dispatch.
    closed = _disposition([_candidate(lifecycle="closed_unmerged", provenance="verified_cross_reference")])
    assert closed["reason_codes"] == ["closed_unmerged_candidate"]

    # A lone historical merged candidate (no current exact candidate) keeps the
    # existing state-specific meaning.
    lone = _disposition([_historical_merged()])
    assert lone["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert lone["reason_codes"] == ["legacy_or_later_scope_expansion"]

    # implementation_already_landed: merged exact-current alone.
    landed = _disposition([_historical_merged(scope_coverage=_current_exact_coverage())])
    assert landed["disposition"] == "implementation_already_landed"

    # already_satisfied precedence still composes over no-authority results
    # (no candidate / closed-unmerged) and never overrides a legacy merged
    # candidate's `legacy_or_later_scope_expansion` result.
    for no_authority in (_disposition([]), closed):
        composed = mod.apply_already_satisfied_precedence(
            no_authority,
            next_action_route="proceed_to_step_1",
            product_spec_routing_action="continue",
            pr_exists=False,
            base_ac_satisfied=True,
            route_loop_verdict_v2_module=route_mod,
        )
        assert composed["disposition"] == "already_satisfied"
    assert (
        mod.apply_already_satisfied_precedence(
            lone,
            next_action_route="proceed_to_step_1",
            product_spec_routing_action="continue",
            pr_exists=False,
            base_ac_satisfied=True,
            route_loop_verdict_v2_module=route_mod,
        )
        == lone
    )
    resumed = _disposition([_historical_merged(), _current_exact()])
    assert (
        mod.apply_already_satisfied_precedence(
            resumed,
            next_action_route="proceed_to_step_1",
            product_spec_routing_action="continue",
            pr_exists=True,
            base_ac_satisfied=True,
            route_loop_verdict_v2_module=route_mod,
        )
        == resumed
    )


# ---------------------------------------------------------------------------
# #2893 PR #2894 review fix_delta (P3): the #2893 carve-out requires a
# positively verified same-Issue `marker` Mapping. A coverage whose `marker`
# key is absent (status/errors otherwise valid) never satisfies the carve-out.
# ---------------------------------------------------------------------------


def _without_marker(coverage):
    stripped = {key: value for key, value in coverage.items() if key != "marker"}
    assert "marker" not in stripped
    assert stripped["status"] in {"covered_exactly", "later_scope_expansion"}
    assert stripped["errors"] == []
    return stripped


def test_p3_valid_same_issue_scope_coverage_requires_marker_mapping_for_target_issue():
    valid = _historical_merged()
    assert mod._has_valid_same_issue_scope_coverage(valid, ISSUE) is True
    # marker key absent -> not valid
    absent = _historical_merged(scope_coverage=_without_marker(_historical_coverage()))
    assert mod._has_valid_same_issue_scope_coverage(absent, ISSUE) is False
    # marker present but not a Mapping / names another Issue -> not valid
    for bad_marker in (None, "marker", ["x"], {"issue_number": ISSUE + 1}, {}):
        coverage = dict(_historical_coverage())
        coverage["marker"] = bad_marker
        candidate = _historical_merged(scope_coverage=coverage)
        assert mod._has_valid_same_issue_scope_coverage(candidate, ISSUE) is False, bad_marker


def test_p3_historical_side_marker_key_missing_does_not_form_carve_out():
    """status=later_scope_expansion / errors=[] but no `marker` key on the
    historical merged candidate: the #2893 carve-out does not apply, so the
    two-candidate conflict is preserved."""
    historical = _historical_merged(scope_coverage=_without_marker(_historical_coverage()))
    assert mod._is_historical_merged_later_scope_candidate(historical, ISSUE) is False
    result = _disposition([historical, _current_exact()])
    assert result["disposition"] == "reconciliation_required"
    assert result["reason_codes"] == ["qualified_candidate_conflict"]
    # Sanity: the same pair with the marker attached resumes.
    assert _disposition([_historical_merged(), _current_exact()])["disposition"] == "existing_pr_resume"


def test_p3_current_side_marker_key_missing_does_not_form_carve_out():
    """status=covered_exactly / errors=[] but no `marker` key on the current
    exact candidate: it is not a current exact resume candidate."""
    current = _current_exact(scope_coverage=_without_marker(_current_exact_coverage()))
    assert mod._is_current_exact_resume_candidate(current, ISSUE) is False
    for order in ((_historical_merged(), current), (current, _historical_merged())):
        result = _disposition(list(order))
        assert result["disposition"] == "reconciliation_required"
        assert result["reason_codes"] == ["qualified_candidate_conflict"]


def test_p3_marker_key_missing_does_not_change_non_2893_paths():
    """#2750 sibling carve-out, markerless legacy resume and a lone
    covered_exactly open candidate without a `marker` key keep their
    pre-#2893 semantics."""
    # #2750: identity-mismatch sibling (no marker key by construction) is still excluded.
    sibling = _candidate(
        lifecycle="merged",
        provenance="verified_cross_reference",
        pr_number=2735,
        scope_coverage=_sibling_identity_mismatch_coverage(),
    )
    assert "marker" not in sibling["scope_coverage"]
    lone_exact = _current_exact(scope_coverage=_without_marker(_current_exact_coverage()))
    result = _disposition([sibling, lone_exact])
    assert result["disposition"] == "existing_pr_resume"
    assert result["candidate"]["pr"]["number"] == 2888
    # Markerless legacy open candidate (no coverage at all) still resumes by ownership.
    legacy = _candidate(lifecycle="open", provenance="verified_cross_reference", pr_number=2890, ownership=True)
    legacy_result = _disposition([legacy])
    assert legacy_result["disposition"] == "existing_pr_resume"
    assert legacy_result["reason_codes"] == ["markerless_allowed_paths_coverage"]
