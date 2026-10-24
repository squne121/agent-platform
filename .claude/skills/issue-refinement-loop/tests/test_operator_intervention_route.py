"""Issue #2397 -- `route_canonical_step2_result()` operator intervention
route.

`merged_review_result.failure_class == "contract_readiness_human_judgment"`
marks an environment/tool/timeout/unknown-classification readiness state
(`contract_readiness_check.py` -- e.g. `env_missing_dep`, `timeout`,
`unknown` classification, `package_manager_no_tty_prompt`, regression-gate
failure, validator internal/timeout errors, body-retrieval failure) that a
Step 4 Issue-body rewrite cannot fix. This is an operator-intervention
condition, not a genuine semantic/owner-ambiguity judgment call (that
remains `STEP_5_HUMAN_JUDGMENT_REQUIRED`, unchanged by this Issue).

NOTE (Issue #2397 Scope Delta, OWNER PR #2398 review P0-1): `command_not_allowed`
(a VC using a command shape that is not on the static preflight allowlist)
is deliberately EXCLUDED from the example list above -- it is now mapped to
`readiness_status: needs_fix` (see `contract_readiness_check.py`'s
`_PREFLIGHT_CATEGORY_TO_READINESS`), not `human_judgment`, because it is
body-author-fixable (rewrite the VC to an allowlisted command). The
`command_not_allowed` -> `STEP_4` regression coverage for that is in
`test_operator_intervention_route_scope_delta.py::
test_command_not_allowed_readiness_routes_to_step_4_not_operator_intervention`
(AC10), and the genuinely-operator-only fixture below uses an `unknown`
classification (`jq` against a nonexistent path) instead.

Verifies:

* AC1: a `status: ok` + `verdict: needs-fix` + `next_action: request_changes`
  payload whose SAME-call `merged_review_result.failure_class` is exactly
  `"contract_readiness_human_judgment"` routes to
  `STEP_5_OPERATOR_INTERVENTION_REQUIRED`, both at the pure-function level
  (`route_canonical_step2_result()` called directly) and via a REAL
  `_cmd_produce()` CLI invocation (the whole real checker subprocess chain
  -- `check_issue_contract.py` -> `contract_readiness_check.py` ->
  `merge_readiness` -> `reviewer_transport.run_reviewer_transport()` ->
  `build_compact_v2()`/`validate_compact_v2()` -> artifact write/verify ->
  `_cmd_produce()`'s own independent readback -- runs for real; only the
  live GitHub body fetch is monkeypatched, matching the existing
  `test_root_review_canonical_delivery.py` E2E pattern). This is the
  reachability proof the Issue #2397 background (OWNER PR #2391 review
  P0-1) requires: the routing decision is exercised against the SAME
  payload shape the real producer emits, not a hand-authored stand-in.
* AC2: the same `needs-fix` + `request_changes` triple with
  `merged_review_result` missing OR its `failure_class` missing/`None`
  still routes to `STEP_4` (Issue #2397 does not change this existing
  default; a body-rewrite-fixable `needs-fix` result has no readiness
  `failure_class` at all).
* AC3: an unrecognized non-empty `failure_class`, or a non-`Mapping`
  `merged_review_result`, fails closed
  (`FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE`) instead of silently
  falling through to `STEP_4` -- an unknown/malformed readiness signal must
  never be treated as an ordinary body-rewrite-fixable defect.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from _trusted_repo import TRUSTED_REPO  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location(
        "run_root_review_pipeline_operator_intervention_route", PIPELINE_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline_operator_intervention_route", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()
_REPO = TRUSTED_REPO

# Issue #2397 Scope Delta (OWNER PR #2398 review P0-1): this fixture MUST NOT
# use a `command_not_allowed` VC shape (a command not on
# `baseline_vc_preflight.py`'s static allowlist, e.g. an unknown binary) --
# that category is now `needs_fix` (body-author-fixable: rewrite the VC to an
# allowlisted command form), not a genuine operator-only condition, and using
# it here would make this "genuinely operator-only" fixture silently stop
# proving anything the moment the P0-1 fix above landed (that is exactly the
# regression the OWNER flagged: this fixture used to be `command_not_allowed`
# and would have kept "passing" against the OLD, pre-fix mapping while no
# longer reaching the operator-intervention route for real).
#
# `jq` IS on the closed allowlist (`_ALLOWED_COMMANDS`; no
# `_is_allowed_jq_invocation`-style sub-validation restricts it further), so
# the real checker chain actually EXECUTES it against a path that does not
# exist. `jq`'s own exit code (2) and stderr ("Could not open file ...: No
# such file or directory") do not match ANY of `classify_result()`'s
# `rg` / `pytest` / `python3` / `node` / `./`-script specific patterns (those
# all require a DIFFERENT `cmd_basename` or a `./`/`../` path substring), so
# classification falls all the way through to the terminal "Unknown: cannot
# classify" branch, which returns `decision: "human_judgment"` directly
# (`category: "unknown"`) -- `map_preflight_result_to_errors()` takes the
# `decision == "human_judgment"` branch UNCONDITIONALLY, never consulting
# `_PREFLIGHT_CATEGORY_TO_READINESS` at all for this result, so this fixture
# stays a genuine operator-only signal regardless of any future
# `_PREFLIGHT_CATEGORY_TO_READINESS` entry changes (Issue #2397 Out of
# Scope: `env_missing_dep` / `timeout` / `package_manager_no_tty_prompt` /
# `unknown` remain operator-only). `merge_readiness_into_review_result()`
# then sets `merged["failure_class"] = "contract_readiness_human_judgment"`
# (`readiness_status_to_failure_class()`) and force-upgrades a would-be
# `verdict: approve` to `verdict: needs-fix` so the failure_class is never
# silently dropped -- producing a genuine, deterministic, hermetic
# `needs-fix` + `request_changes` + `contract_readiness_human_judgment`
# payload with no live GitHub / network / missing-binary dependency (`jq`
# itself is a real, locally-installed binary; only the JSON file path it is
# given does not exist).
_CONTRACT_READINESS_HUMAN_JUDGMENT_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2397 operator intervention route fixture (contract_readiness_human_judgment branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
`route_canonical_step2_result()` routes a genuine
`merged_review_result.failure_class: contract_readiness_human_judgment`
result to `STEP_5_OPERATOR_INTERVENTION_REQUIRED` (Issue #2397).

## Acceptance Criteria

- [ ] AC1: fixture body's Verification Command uses an allowlisted binary
      (`jq`) against a path that does not exist, so the real checker chain
      actually executes it and its readiness check classifies the result
      `category: unknown` / `decision: human_judgment` (genuinely
      operator-only, NOT the body-fixable `command_not_allowed` category),
      producing a genuine
      `merged_review_result.failure_class: contract_readiness_human_judgment`.

## Verification Commands

```bash
# AC1
$ jq '.' fixture/e2e_produce_operator_intervention_unknown_classification.json
```

## Allowed Paths

- fixture/e2e_produce_operator_intervention_unknown_classification.json
"""


def _run_real_produce(tmp_path: Path, monkeypatch, *, body: str, issue_number: int) -> dict:
    """Run a REAL `_cmd_produce()` invocation (real checker subprocess chain
    via `reviewer_transport.run_reviewer_transport()`); only the live GitHub
    body fetch is replaced with a pinned fixture, matching the existing
    `test_root_review_canonical_delivery.py` E2E pattern."""
    body_sha256 = _PIPELINE.sha256_of(body)
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return body, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    args = argparse.Namespace(issue_number=issue_number, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())
    assert rc == 0, out
    assert out["status"] == "ok", out
    return out


# ---------------------------------------------------------------------------
# AC1: `needs-fix` + `request_changes` + `merged_review_result.failure_class
# == "contract_readiness_human_judgment"` routes to
# `STEP_5_OPERATOR_INTERVENTION_REQUIRED`.
# ---------------------------------------------------------------------------


def test_route_canonical_step2_result_needs_fix_request_changes_with_contract_readiness_human_judgment_routes_to_operator_intervention():  # noqa: E501
    result = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": {"failure_class": "contract_readiness_human_judgment"},
    }
    assert (
        _PIPELINE.route_canonical_step2_result(result)
        == _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED
    )
    assert _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED != _PIPELINE.STEP_5_HUMAN_JUDGMENT_REQUIRED
    assert _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED != _PIPELINE.STEP_4


def test_given_real_produce_needs_fix_body_with_contract_readiness_human_judgment_when_run_then_routes_to_operator_intervention(  # noqa: E501
    tmp_path: Path, monkeypatch
):
    out = _run_real_produce(
        tmp_path,
        monkeypatch,
        body=_CONTRACT_READINESS_HUMAN_JUDGMENT_BODY,
        issue_number=2397001,
    )
    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "needs-fix", out
    # Issue #2397 Out of Scope: the compact V2 wire's `NEXT_ACTION` two-value
    # contract (`proceed | request_changes`) is unchanged by this Issue --
    # NOT a new `human_judgment_required` wire value.
    assert compact_result["next_action"] == "request_changes", out
    assert (
        out["merged_review_result"]["failure_class"] == "contract_readiness_human_judgment"
    ), out
    assert out["canonical_step2_route"] == _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED, out


# ---------------------------------------------------------------------------
# AC2: `needs-fix` + `request_changes` with `merged_review_result` missing or
# its `failure_class` missing/`None` still routes to `STEP_4` (existing
# default, unchanged).
# ---------------------------------------------------------------------------


def test_route_canonical_step2_result_needs_fix_request_changes_without_failure_class_routes_to_step_4():
    no_merged_key = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
    }
    assert _PIPELINE.route_canonical_step2_result(no_merged_key) == _PIPELINE.STEP_4

    merged_none = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": None,
    }
    assert _PIPELINE.route_canonical_step2_result(merged_none) == _PIPELINE.STEP_4

    failure_class_missing = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": {"blocking_issues": ["some C3 fix"]},
    }
    assert _PIPELINE.route_canonical_step2_result(failure_class_missing) == _PIPELINE.STEP_4

    failure_class_none = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": {"failure_class": None},
    }
    assert _PIPELINE.route_canonical_step2_result(failure_class_none) == _PIPELINE.STEP_4


# ---------------------------------------------------------------------------
# AC3: an unrecognized non-empty `failure_class`, or a non-`Mapping`
# `merged_review_result`, fails closed instead of falling through to
# `STEP_4`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "merged_review_result",
    [
        {"failure_class": "some_unrecognized_failure_class"},
        {"failure_class": ""},
        ["unexpected", "list"],
        "unexpected string",
        1,
    ],
    ids=[
        "unknown_non_empty_failure_class",
        "empty_string_failure_class",
        "merged_review_result_list",
        "merged_review_result_string",
        "merged_review_result_int",
    ],
)
def test_route_canonical_step2_result_needs_fix_request_changes_with_unknown_failure_class_fails_closed(
    merged_review_result,
):
    result = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": merged_review_result,
    }
    assert (
        _PIPELINE.route_canonical_step2_result(result)
        == _PIPELINE.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE
    )
