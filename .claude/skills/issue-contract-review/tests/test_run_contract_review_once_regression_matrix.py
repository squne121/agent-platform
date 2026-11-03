"""
tests/test_run_contract_review_once_regression_matrix.py

Issue #1914 fix_delta (PR #1940 adversarial review, P0-1 / P0-3 / P1-2):

Regression matrix requested by the review that the original two new test
files (test_run_contract_review_once_delivery_rollup_skip.py /
test_run_contract_review_once_implementation_still_blocked.py) did not
cover:

  - delivery-rollup parent WITH a passing `## Verification Commands`
    section -> VC actually executes (not skipped)
  - delivery-rollup parent WITH a failing VC -> failure is NOT ignored
  - `quality-gate` parent_mode + no VC section -> NOT exempt
  - `research` issue_kind + no VC section -> behaves as before (not exempt)
  - unknown/unrecognized issue_kind -> behaves as before (not exempt)
  - malformed Machine-Readable Contract (unparseable YAML) -> not silently
    exempted
  - duplicate key in the MRC block -> not silently exempted
  - a decoy YAML block elsewhere in the body is not misparsed as the real
    MRC
  - Step 2 / Step 4.5 body SHA mismatch (P0-3): after the P0-3 fix there is
    only ONE fetch_body_from_github() call per run_once() invocation ->
    verified directly via call-count assertion (structurally impossible
    mismatch, not merely detected)
  - P0-1: the `applicability` top-level field distinguishes a delivery-
    rollup "not applicable" go from a normal VC-pass "applicable" go

Runtime Verification Applicability: not_applicable (static / pytest
regression tests only, matching Issue #1914's own Runtime Verification
Applicability section).
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once

_ISSUE_NUMBER = 1940
_REPO = "squne121/agent-platform"


# ---------------------------------------------------------------------------
# Fixture bodies
# ---------------------------------------------------------------------------


def _delivery_rollup_body(*, with_vc: str | None = None, extra: str = "") -> str:
    vc_section = (
        textwrap.dedent(
            f"""
            ## Verification Commands

            ```bash
            # AC1
            $ {with_vc}
            ```
            """
        )
        if with_vc
        else ""
    )
    return textwrap.dedent(
        """\
        ## Machine-Readable Contract

        ```yaml
        contract_schema_version: v1
        issue_kind: parent
        parent_mode: delivery-rollup
        closure_mode: child-complete
        ```

        ## Summary

        fixture delivery-rollup parent.

        ## Acceptance Criteria

        - [ ] AC1: fixture
        """
    ) + vc_section + extra


def _parent_body(parent_mode: str) -> str:
    return textwrap.dedent(
        f"""\
        ## Machine-Readable Contract

        ```yaml
        contract_schema_version: v1
        issue_kind: parent
        parent_mode: {parent_mode}
        closure_mode: measurement-ready
        ```

        ## Summary

        fixture {parent_mode} parent (not delivery-rollup).

        ## Acceptance Criteria

        - [ ] AC1: fixture
        """
    )


def _research_body() -> str:
    return textwrap.dedent(
        """\
        ## Machine-Readable Contract

        ```yaml
        contract_schema_version: v1
        issue_kind: research
        parent_issue: "none"
        ```

        ## Outcome

        fixture research issue, no Verification Commands section.
        """
    )


def _unknown_kind_body() -> str:
    return textwrap.dedent(
        """\
        ## Machine-Readable Contract

        ```yaml
        contract_schema_version: v1
        issue_kind: totally_unknown_kind
        parent_issue: "none"
        ```

        ## Outcome

        fixture with an unrecognized issue_kind.
        """
    )


def _malformed_mrc_body() -> str:
    return textwrap.dedent(
        """\
        ## Machine-Readable Contract

        ```yaml
        contract_schema_version: v1
        issue_kind: parent
        parent_mode: [this, is, not, a, mapping
        ```

        ## Summary

        fixture with unparseable YAML in the MRC block.
        """
    )


def _duplicate_key_mrc_body() -> str:
    return textwrap.dedent(
        """\
        ## Machine-Readable Contract

        ```yaml
        contract_schema_version: v1
        issue_kind: parent
        parent_mode: delivery-rollup
        parent_mode: quality-gate
        closure_mode: child-complete
        ```

        ## Summary

        fixture with a duplicated parent_mode key in the MRC block.
        """
    )


def _decoy_yaml_body() -> str:
    """Real MRC is delivery-rollup; a decoy fenced yaml block elsewhere in
    the body declares a DIFFERENT parent_mode. The section-bound MRC parser
    (mrc_contract_parser.py) must not be confused by it."""
    return textwrap.dedent(
        """\
        ## Machine-Readable Contract

        ```yaml
        contract_schema_version: v1
        issue_kind: parent
        parent_mode: delivery-rollup
        closure_mode: child-complete
        ```

        ## Summary

        fixture with a decoy yaml block below.

        ## Notes

        Example of what NOT to write:

        ```yaml
        issue_kind: parent
        parent_mode: quality-gate
        ```

        The above is a decoy, not the real Machine-Readable Contract.
        """
    )


# ---------------------------------------------------------------------------
# Helpers (mirroring test_run_contract_review_once.py's fixture generators)
# ---------------------------------------------------------------------------


def _make_readiness_go_json() -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "go",
        "body_sha256": "sha256:abc",
        "source_checks": [],
        "errors": [],
        "minimal_context": [],
        "fix_hint": None,
    }


def _make_readiness_blocked_json() -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "needs_fix",
        "body_sha256": "sha256:abc",
        "source_checks": [],
        "errors": [{"rule_id": "VC001_NO_VERIFICATION_COMMANDS_SECTION"}],
        "minimal_context": [],
        "fix_hint": None,
    }


def _make_product_spec_not_applicable_json() -> dict:
    return {
        "schema": "product_spec_check/v1",
        "applicability": "not_applicable",
        "decision": "pass",
        "triggers": {},
        "conditions": {},
        "blocked_reasons": [],
        "body_sha256": "sha256:abc",
        "source_provenance": {"source_type": "github_issue_body", "body_file": None},
    }


def _make_vc_preflight_json(status: str) -> dict:
    return {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "status": status,
        "results": [],
        "errors": [],
    }


def _make_declared_path_overlap_stub() -> dict:
    return {
        "schema": "declared_path_overlap/v1",
        "advisory": True,
        "blocking": False,
        "decision": "unavailable",
        "disjoint": None,
        "overlapping_prs": [],
        "inventory": None,
        "errors": [],
    }


def _run_with_mocks(body: str, run_script_results: list, shell_result=(0, "OK: no blockers", "")):
    """Run run_once() with the standard set of mocks, recording every
    fetch_body_from_github() call so tests can assert call count (P0-3)."""
    fetch_calls: list[tuple] = []

    def _fetch(issue_number, repo):
        fetch_calls.append((issue_number, repo))
        return (body, None)

    run_iter = iter(run_script_results)

    with patch.object(_rcr_mod, "fetch_body_from_github", side_effect=_fetch):
        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=shell_result):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_stub(),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

    return result, fetch_calls


# ---------------------------------------------------------------------------
# 1. delivery-rollup parent WITH passing VC -> VC actually executes
# ---------------------------------------------------------------------------


class TestDeliveryRollupWithPassingVc:
    def test_vc_actually_executes_and_passes(self):
        body = _delivery_rollup_body(with_vc="echo ok")
        result, fetch_calls = _run_with_mocks(
            body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
                (_make_vc_preflight_json("pass"), 0, None),
            ],
        )
        assert result["status"] == "go"
        assert result["source"] == "all_checks_pass"
        assert result["checks"]["vc_preflight"] == "pass"
        assert result["applicability"] == "applicable"
        assert len(fetch_calls) == 1


class TestDeliveryRollupWithFailingVc:
    def test_vc_failure_is_not_ignored(self):
        body = _delivery_rollup_body(with_vc="false")
        result, fetch_calls = _run_with_mocks(
            body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
                (_make_vc_preflight_json("blocked"), 1, None),
            ],
        )
        assert result["status"] == "blocked"
        assert result["source"] == "vc_preflight"
        assert result["checks"]["vc_preflight"] == "blocked"
        assert len(fetch_calls) == 1


# ---------------------------------------------------------------------------
# 2. non-delivery-rollup parent_mode is NOT exempt
# ---------------------------------------------------------------------------


class TestNonDeliveryRollupParentModesNotExempt:
    def test_quality_gate_parent_mode_not_exempt(self):
        body = _parent_body("quality-gate")
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)

        result, _ = _run_with_mocks(
            body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
                (_make_vc_preflight_json("blocked"), 1, None),
            ],
        )
        # unchanged behavior: VC preflight still runs and can still block.
        assert result["status"] == "blocked"
        assert result["source"] == "vc_preflight"

    def test_routing_map_parent_mode_not_exempt(self):
        body = _parent_body("routing-map")
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)

    def test_decision_log_parent_mode_not_exempt(self):
        body = _parent_body("decision-log")
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)


class TestResearchAndUnknownKindNotExempt:
    def test_research_issue_kind_not_exempt(self):
        body = _research_body()
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)

    def test_unknown_issue_kind_not_exempt(self):
        body = _unknown_kind_body()
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)


class TestMalformedOrDuplicateKeyMrcNotExempt:
    def test_malformed_mrc_not_exempt(self):
        body = _malformed_mrc_body()
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)

    def test_duplicate_key_mrc_not_exempt(self):
        body = _duplicate_key_mrc_body()
        assert not _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)


class TestDecoyYamlBlockNotMisparsed:
    def test_decoy_yaml_block_does_not_override_real_mrc(self):
        """The real (delivery-rollup) MRC still wins even with a decoy yaml
        block elsewhere in the body declaring a different parent_mode."""
        body = _decoy_yaml_body()
        assert _rcr_mod._is_delivery_rollup_parent_without_vc_section(body)


# ---------------------------------------------------------------------------
# 3. P0-3: single fetch, structurally no Step2/Step4.5 mismatch possible
# ---------------------------------------------------------------------------


class TestSingleBodyFetchP0_3:
    def test_delivery_rollup_go_path_fetches_body_exactly_once(self):
        body = _delivery_rollup_body()
        result, fetch_calls = _run_with_mocks(
            body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
            ],
        )
        assert result["status"] == "go"
        assert result["source"] == "delivery_rollup_parent_without_verification_commands"
        assert len(fetch_calls) == 1, (
            "run_once() must fetch the Issue body exactly once per "
            "invocation (Issue #1914 P0-3); Step 2 and Step 4.5 must reuse "
            "the same snapshot, not each independently re-fetch."
        )

    def test_body_snapshot_fetch_failure_is_runtime_error(self):
        with patch.object(
            _rcr_mod, "fetch_body_from_github", return_value=(None, "gh_timeout")
        ):
            result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert any("body_snapshot_fetch_error" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# 4. P0-1: applicability field distinguishes not_applicable from applicable
# ---------------------------------------------------------------------------


class TestApplicabilityFieldP0_1:
    def test_delivery_rollup_go_has_not_applicable_applicability(self):
        body = _delivery_rollup_body()
        result, _ = _run_with_mocks(
            body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
            ],
        )
        assert result["status"] == "go"
        assert result["applicability"] == "not_applicable"

    def test_normal_vc_pass_go_has_applicable_applicability(self):
        body = _delivery_rollup_body(with_vc="echo ok")
        result, _ = _run_with_mocks(
            body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
                (_make_vc_preflight_json("pass"), 0, None),
            ],
        )
        assert result["status"] == "go"
        assert result["applicability"] == "applicable"

    def test_status_go_alone_is_ambiguous_but_applicability_disambiguates(self):
        """P0-1 (#1940 review): a consumer reading ONLY `status` cannot
        distinguish these two cases. This test documents (and fixes as an
        executable regression) that the FULL result carries a
        distinguishing field for any consumer that inspects it.

        Known gap (explicitly out of scope for this PR, not silently
        expanded): impl-review-loop's routing and implement-issue's dispatch
        condition are documented (see steps/preparation.md and
        implement-issue/SKILL.md) to read only `CONTRACT_REVIEW_RESULT_V1
        status`. Those consumer files live outside this Issue's Allowed
        Paths (only run_contract_review_once.py + termination-policy.md +
        this tests/ directory are listed) and were NOT modified by this fix
        -- updating them to consume `applicability` is a larger, separate
        scope decision left to a follow-up Issue / explicit human review,
        not something this PR silently expands into.
        """
        delivery_rollup_body = _delivery_rollup_body()
        normal_pass_body = _delivery_rollup_body(with_vc="echo ok")

        delivery_rollup_result, _ = _run_with_mocks(
            delivery_rollup_body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
            ],
        )
        normal_pass_result, _ = _run_with_mocks(
            normal_pass_body,
            [
                (_make_readiness_go_json(), 0, None),
                (_make_product_spec_not_applicable_json(), 0, None),
                (_make_vc_preflight_json("pass"), 0, None),
            ],
        )

        # Both are indistinguishable if a consumer reads status alone:
        assert delivery_rollup_result["status"] == "go"
        assert normal_pass_result["status"] == "go"
        # ...but the full result carries a distinguishing field:
        assert delivery_rollup_result["applicability"] != normal_pass_result["applicability"]
        assert delivery_rollup_result["applicability"] == "not_applicable"
        assert normal_pass_result["applicability"] == "applicable"

    def test_idempotency_dedupe_propagates_not_applicable_applicability(self):
        """P0-1: a deduped delivery-rollup go (idempotency Step 1 reuse
        path) must still carry applicability: not_applicable, derived from
        the saved comment's checks.vc_preflight marker."""
        existing_go = {
            "html_url": "https://github.com/squne121/agent-platform/issues/1940#issuecomment-1",
            "inner": {
                "checks": {
                    "product_spec_check": _make_product_spec_not_applicable_json(),
                    "vc_preflight": "not_applicable",
                }
            },
        }
        with patch.object(_rcr_mod, "fetch_body_from_github", return_value=(_delivery_rollup_body(), None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(existing_go, None)):
                with patch.object(
                    _rcr_mod,
                    "_run_declared_path_overlap_check",
                    return_value=_make_declared_path_overlap_stub(),
                ):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        assert result["status"] == "go"
        assert result["source"] == "existing_go_comment"
        assert result["applicability"] == "not_applicable"

    def test_idempotency_dedupe_defaults_to_applicable_for_normal_go(self):
        existing_go = {
            "html_url": "https://github.com/squne121/agent-platform/issues/1940#issuecomment-2",
            "inner": {
                "checks": {
                    "product_spec_check": _make_product_spec_not_applicable_json(),
                    "vc_preflight": "pass",
                }
            },
        }
        body = _delivery_rollup_body(with_vc="echo ok")
        with patch.object(_rcr_mod, "fetch_body_from_github", return_value=(body, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(existing_go, None)):
                with patch.object(
                    _rcr_mod,
                    "_run_declared_path_overlap_check",
                    return_value=_make_declared_path_overlap_stub(),
                ):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        assert result["status"] == "go"
        assert result["applicability"] == "applicable"


# ---------------------------------------------------------------------------
# 5. P1-1: shared DeliveryRollupApplicability dataclass is consumed, not
#    recombined ad hoc at the call site.
# ---------------------------------------------------------------------------


class TestSharedApplicabilityResolverP1_1:
    def test_resolve_delivery_rollup_applicability_returns_reason_code(self):
        result = _rcr_mod._resolve_delivery_rollup_applicability(_delivery_rollup_body())
        assert result.applicable is True
        assert result.issue_kind == "parent"
        assert result.parent_mode == "delivery-rollup"
        assert result.reason_code == "delivery_rollup_parent_without_verification_commands"
        assert result.body_sha256.startswith("sha256:")

    def test_bool_wrapper_delegates_to_shared_resolver(self):
        body = _delivery_rollup_body()
        assert _rcr_mod._is_delivery_rollup_parent_without_vc_section(
            body
        ) == _rcr_mod._resolve_delivery_rollup_applicability(body).applicable

    def test_vc_section_present_reason_code(self):
        body = _delivery_rollup_body(with_vc="echo ok")
        result = _rcr_mod._resolve_delivery_rollup_applicability(body)
        assert result.applicable is False
        assert result.reason_code == "vc_section_present"

    def test_malformed_mrc_reason_code(self):
        """A malformed MRC already fails at
        resolve_existing_issue_validation_profile()'s own internal MRC
        parse (status != "profile"), so the resolver's earliest guard
        clause applies and issue_kind/parent_mode/reason_code all stay
        unset -- it never reaches the exemption-specific reason codes.
        Not exempt either way (applicable is False)."""
        result = _rcr_mod._resolve_delivery_rollup_applicability(_malformed_mrc_body())
        assert result.applicable is False
        assert result.issue_kind is None
        assert result.parent_mode is None
