"""Issue #2397 Scope Delta (OWNER PR #2398 review, https://github.com/
source-repo PR #2398 comment 5465119493) -- P0-1/P1-1/P1-2
regression coverage.

The OWNER's PR #2398 review found that the original Issue #2397
implementation's E2E fixture used a `command_not_allowed` VC shape as its
stand-in for "genuinely operator-only, body-unfixable" readiness. That is
wrong: `command_not_allowed` (a VC using a command shape that is not on
`baseline_vc_preflight.py`'s static allowlist) is body-author-fixable --
rewriting the VC to an allowlisted command form resolves it -- unlike a real
`env_missing_dep` / `timeout` / unknown-classification condition. Before this
Scope Delta, `command_not_allowed` was not a key in
`contract_readiness_check._PREFLIGHT_CATEGORY_TO_READINESS` at all, so it
fell through to the module's `human_judgment` default, which in turn made
`route_canonical_step2_result()` misroute an ordinary, body-fixable
`needs-fix` Issue to the operator-intervention route (Step 5) instead of the
normal rewrite loop (Step 4).

Verifies:

* AC9: `command_not_allowed` now maps to `readiness_status: needs_fix`, not
  `human_judgment` -- both directly against the
  `_PREFLIGHT_CATEGORY_TO_READINESS` dict and via
  `map_preflight_result_to_errors()` given a realistic `command_not_allowed`
  preflight result item.
* AC10: a genuine `command_not_allowed` readiness result (no `failure_class`)
  reaches `route_canonical_step2_result()`'s `STEP_4` route, NOT
  `STEP_5_OPERATOR_INTERVENTION_REQUIRED`, via a REAL `_cmd_produce()`
  invocation (the whole real checker subprocess chain, matching
  `test_operator_intervention_route.py`'s E2E pattern).
* AC11: a genuinely operator-only fixture (NOT `command_not_allowed`) still
  reaches `STEP_5_OPERATOR_INTERVENTION_REQUIRED` via the same real
  `_cmd_produce()` path, proving the P0-1 fix did not also break the
  legitimate operator-intervention route.
* AC12: `_cmd_produce()`'s stdout JSON carries `canonical_step2_disposition`
  alongside `canonical_step2_route` -- a terminal disposition
  (`terminal: true` / `termination_reason: "human_escalation"` /
  `termination_cause: "operator_intervention_required"`) only for the
  operator-intervention route, and `{"route": route}` only otherwise.
* AC14 (Scope Delta iteration 3, OWNER PR #2398 review, 3rd round, P0-1):
  AC11's "genuinely operator-only" fixture no longer relies on the
  `unknown` catch-all classification (`jq` against a hardcoded nonexistent
  path). `unknown` is a classifier-gap catch-all that also absorbs
  body-fixable conditions (typo / stale VC / classifier gap), so it does
  not actually PROVE the readiness state is operator-only. AC11's fixture
  now forces a KNOWN, named operator-only category (`env_missing_dep`) by
  shimming an allowlisted command's real subprocess execution boundary
  (see `_write_forced_env_missing_dep_command()` below) instead of relying
  on ambient host/classifier behavior.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from _trusted_repo import TRUSTED_REPO  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PIPELINE_SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = PIPELINE_SCRIPTS_DIR / "run_root_review_pipeline.py"
CRC_SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
CRC_SCRIPT = CRC_SCRIPTS_DIR / "contract_readiness_check.py"

if str(PIPELINE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_SCRIPTS_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_module("run_root_review_pipeline_operator_intervention_route_scope_delta", PIPELINE_SCRIPT)
_CRC = _load_module("contract_readiness_check_operator_intervention_route_scope_delta", CRC_SCRIPT)

_REPO = TRUSTED_REPO


def _run_real_produce(tmp_path: Path, monkeypatch, *, body: str, issue_number: int) -> dict:
    """Run a REAL `_cmd_produce()` invocation (real checker subprocess chain
    via `reviewer_transport.run_reviewer_transport()`); only the live GitHub
    body fetch is replaced with a pinned fixture, matching the existing
    `test_operator_intervention_route.py` E2E pattern."""
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


def _write_forced_env_missing_dep_command(
    tmp_path: Path, monkeypatch, *, binary_name: str = "jq"
) -> None:
    """Issue #2397 Scope Delta iteration 3 (OWNER PR #2398 review, 3rd
    round, P0-1): deterministically force a KNOWN operator-only preflight
    classification (`category: "env_missing_dep"`) at the REAL preflight
    subprocess execution boundary, instead of relying on the `unknown`
    catch-all (which also absorbs body-fixable typo/stale-VC/classifier-gap
    conditions and therefore does not prove genuine operator-only-ness).

    Mechanism: `binary_name` (`jq` by default) IS on
    `baseline_vc_preflight.py`'s closed `_ALLOWED_COMMANDS` allowlist, so a
    VC invoking it passes the static `command_not_allowed` check and
    reaches REAL execution. This shims that binary: a directory containing
    an executable named `binary_name` that unconditionally `exit`s 127 is
    prepended to `PATH`. Every layer of the real checker subprocess chain
    `_run_real_produce()` exercises (`reviewer_transport.
    run_reviewer_transport()` -> `run_root_review_pipeline.py
    run-checker-attempt` -> `check_issue_contract.py` ->
    `contract_readiness_check.run_baseline_vc_preflight()` ->
    `baseline_vc_preflight.py`'s own VC-command `subprocess.Popen()`)
    launches its child with `env=None` (inherit current environment), so
    this shimmed `PATH` propagates all the way down to the actual
    VC-command exec -- deterministically, regardless of whether the host
    machine happens to have a real `jq` installed or what its real
    behavior against any particular input would be.
    `classify_result()`'s `exit_code in (126, 127)` branch classifies this
    `category: "env_missing_dep"` unconditionally (independent of stderr
    wording), which `_PREFLIGHT_CATEGORY_TO_READINESS` maps to
    `human_judgment` -- one of the Issue #2397 Out of Scope's confirmed
    operator-only categories (`env_missing_dep` / `timeout` /
    `package_manager_no_tty_prompt` / `unknown` remain operator-only).
    """
    fake_bin_dir = tmp_path / "fake-bin-env-missing-dep"
    fake_bin_dir.mkdir(parents=True, exist_ok=True)
    shim_path = fake_bin_dir / binary_name
    shim_path.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    shim_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


# `zzz-not-a-real-preflight-allowlisted-tool-2397` is not on
# `baseline_vc_preflight.py`'s closed allowlist (`_ALLOWED_COMMANDS`), so the
# real checker chain classifies it `category: command_not_allowed` /
# `decision: blocked` before any attempt to execute it (purely static, no
# exec, no missing-binary lookup, no network).
_COMMAND_NOT_ALLOWED_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2397 Scope Delta AC10 fixture (command_not_allowed -> needs_fix -> step_4)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
a `command_not_allowed` VC (body-author-fixable) routes to `STEP_4`, NOT
`STEP_5_OPERATOR_INTERVENTION_REQUIRED` (Issue #2397 Scope Delta AC10).

## Acceptance Criteria

- [ ] AC1: fixture body's Verification Command uses a binary that is not on
      the VC preflight allowlist, so the real checker chain's readiness
      check classifies it `category: command_not_allowed` -> readiness
      `needs_fix` (Issue #2397 Scope Delta AC9).

## Verification Commands

```bash
# AC1
$ zzz-not-a-real-preflight-allowlisted-tool-2397-scope-delta --version
```

## Allowed Paths

- fixture/e2e_produce_operator_intervention_command_not_allowed.md
"""


# Issue #2397 Scope Delta iteration 3 (OWNER PR #2398 review, 3rd round,
# P0-1): this fixture used to point `jq` at a hardcoded nonexistent path,
# relying on the resulting `category: "unknown"` catch-all classification
# as its stand-in for "genuinely operator-only". That was wrong: `unknown`
# is a classifier-gap catch-all that ALSO absorbs body-fixable conditions
# (typo / stale VC / classifier gap), so it never actually proved the
# readiness state was operator-only -- it only proved the classifier could
# not recognize the failure. The fixture below instead uses a plain,
# innocuous `jq --version` invocation; determinism comes from
# `_write_forced_env_missing_dep_command()` (called by each test using this
# fixture) shimming `jq`'s real subprocess execution boundary via `PATH` so
# the real checker chain deterministically observes exit code 127 ->
# `category: "env_missing_dep"` (a KNOWN, named operator-only category --
# see Issue #2397 Out of Scope), regardless of host/environment state. The
# unreliable `fixture/e2e_produce_operator_intervention_scope_delta_unknown.json`
# reference has been removed entirely.
_GENUINELY_OPERATOR_ONLY_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2397 Scope Delta AC11/AC14 fixture (genuinely operator-only, not command_not_allowed)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
a genuinely operator-only readiness state (NOT `command_not_allowed`) still
routes to `STEP_5_OPERATOR_INTERVENTION_REQUIRED` after the Issue #2397
Scope Delta P0-1 fix (Issue #2397 Scope Delta AC11/AC14).

## Acceptance Criteria

- [ ] AC1: fixture body's Verification Command uses an allowlisted binary
      (`jq`) whose real subprocess execution boundary the test process has
      shimmed (via `PATH`) to deterministically exit 127, so the real
      checker chain's readiness check classifies the result
      `category: env_missing_dep` / `decision: blocked` -> `human_judgment`
      (genuinely operator-only, a KNOWN category, NOT the classifier-gap
      `unknown` catch-all and NOT the body-fixable `command_not_allowed`
      category).

## Verification Commands

```bash
# AC1
$ jq --version
```

## Allowed Paths

- fixture/e2e_produce_operator_intervention_scope_delta_env_missing_dep.md
"""


# A plain, deterministic approve fixture (STEP_2_5 route) used by AC12's
# "non-operator route" disposition check. Mirrors the known-good
# `_APPROVE_BODY` fixture in `test_root_review_canonical_delivery.py`
# (`# baseline-expect: pass` + `$ true`), which the real checker chain
# resolves to a genuine `verdict: approve` / `next_action: proceed`.
_APPROVE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2397 Scope Delta AC12 fixture (approve -> step_2_5, route-only disposition)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
`canonical_step2_disposition` is `{"route": route}` only (no `terminal` /
`termination_reason` / `termination_cause` keys) for a non-operator route
(Issue #2397 Scope Delta AC12).

## Acceptance Criteria

- [ ] AC1: fixture body is well-formed enough for check_issue_contract.py to
      synthesize a complete REVIEW_ISSUE_RESULT_V1 with verdict approve.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/e2e_produce_operator_intervention_scope_delta_approve.md
"""


# ---------------------------------------------------------------------------
# AC9: `command_not_allowed` maps to `needs_fix`, not `human_judgment`.
# ---------------------------------------------------------------------------


def test_command_not_allowed_category_maps_to_needs_fix_not_human_judgment():
    assert _CRC._PREFLIGHT_CATEGORY_TO_READINESS["command_not_allowed"] == "needs_fix"

    # Also verify via the real mapping function (`map_preflight_result_to_errors()`),
    # given the SAME `(classification, category, decision, scope_class)` shape
    # `classify_result()` actually returns for a `command_not_allowed` VC
    # (see `baseline_vc_preflight.py`'s `command_not_allowed` return sites:
    # "blocked", "command_not_allowed", "blocked", <hint>, "baseline_fail_expected").
    preflight_result = {
        "status": "blocked",
        "results": [
            {
                "classification": "blocked",
                "category": "command_not_allowed",
                "decision": "blocked",
                "scope_class": "baseline_fail_expected",
                "command": "zzz-not-a-real-preflight-allowlisted-tool-2397 --version",
            }
        ],
        "errors": [],
    }
    errors, aggregate_status = _CRC.map_preflight_result_to_errors(preflight_result)
    assert aggregate_status == "needs_fix", (errors, aggregate_status)
    assert errors, errors
    assert errors[0]["category"] == "command_not_allowed", errors


# ---------------------------------------------------------------------------
# AC10: `command_not_allowed` readiness (no `failure_class`) routes to
# `STEP_4`, not `STEP_5_OPERATOR_INTERVENTION_REQUIRED`, via a REAL
# `_cmd_produce()` invocation.
# ---------------------------------------------------------------------------


def test_command_not_allowed_readiness_routes_to_step_4_not_operator_intervention(
    tmp_path: Path, monkeypatch
):
    out = _run_real_produce(
        tmp_path,
        monkeypatch,
        body=_COMMAND_NOT_ALLOWED_BODY,
        issue_number=2397101,
    )
    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "needs-fix", out
    assert compact_result["next_action"] == "request_changes", out
    merged_review_result = out["merged_review_result"]
    assert merged_review_result.get("failure_class") is None, out
    assert out["canonical_step2_route"] == _PIPELINE.STEP_4, out
    assert out["canonical_step2_route"] != _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED, out


# ---------------------------------------------------------------------------
# AC11: a genuinely operator-only fixture (not `command_not_allowed`) still
# routes to `STEP_5_OPERATOR_INTERVENTION_REQUIRED`.
# ---------------------------------------------------------------------------


def test_genuinely_operator_only_fixture_routes_to_operator_intervention(
    tmp_path: Path, monkeypatch
):
    _write_forced_env_missing_dep_command(tmp_path, monkeypatch)
    out = _run_real_produce(
        tmp_path,
        monkeypatch,
        body=_GENUINELY_OPERATOR_ONLY_BODY,
        issue_number=2397102,
    )
    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "needs-fix", out
    assert compact_result["next_action"] == "request_changes", out
    merged_review_result = out["merged_review_result"]
    assert merged_review_result["failure_class"] == "contract_readiness_human_judgment", out
    assert out["canonical_step2_route"] == _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED, out


# ---------------------------------------------------------------------------
# AC12: `_cmd_produce()`'s stdout JSON carries `canonical_step2_disposition`.
# ---------------------------------------------------------------------------


def test_cmd_produce_includes_canonical_step2_disposition_for_operator_route(
    tmp_path: Path, monkeypatch
):
    _write_forced_env_missing_dep_command(tmp_path, monkeypatch)
    out = _run_real_produce(
        tmp_path,
        monkeypatch,
        body=_GENUINELY_OPERATOR_ONLY_BODY,
        issue_number=2397103,
    )
    assert out["canonical_step2_route"] == _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED, out
    disposition = out["canonical_step2_disposition"]
    assert disposition == {
        "route": _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED,
        "terminal": True,
        "termination_reason": "human_escalation",
        "termination_cause": "operator_intervention_required",
    }, out


def test_cmd_produce_disposition_is_route_only_for_non_operator_routes(
    tmp_path: Path, monkeypatch
):
    approve_out = _run_real_produce(
        tmp_path,
        monkeypatch,
        body=_APPROVE_BODY,
        issue_number=2397104,
    )
    assert approve_out["canonical_step2_route"] == _PIPELINE.STEP_2_5, approve_out
    assert approve_out["canonical_step2_disposition"] == {"route": _PIPELINE.STEP_2_5}, approve_out

    step4_out = _run_real_produce(
        tmp_path,
        monkeypatch,
        body=_COMMAND_NOT_ALLOWED_BODY,
        issue_number=2397105,
    )
    assert step4_out["canonical_step2_route"] == _PIPELINE.STEP_4, step4_out
    assert step4_out["canonical_step2_disposition"] == {"route": _PIPELINE.STEP_4}, step4_out


def test_build_canonical_step2_disposition_pure_function_contract():
    """Direct unit coverage of `build_canonical_step2_disposition()` itself
    (independent of any real `_cmd_produce()` invocation), covering every
    known route constant."""
    assert _PIPELINE.build_canonical_step2_disposition(
        _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED
    ) == {
        "route": _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED,
        "terminal": True,
        "termination_reason": "human_escalation",
        "termination_cause": "operator_intervention_required",
    }
    for route in (
        _PIPELINE.STEP_2_5,
        _PIPELINE.STEP_4,
        _PIPELINE.STEP_4_5,
        _PIPELINE.STEP_5_HUMAN_JUDGMENT_REQUIRED,
        _PIPELINE.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE,
    ):
        assert _PIPELINE.build_canonical_step2_disposition(route) == {"route": route}
