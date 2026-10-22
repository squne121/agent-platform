"""
.claude/skills/issue-refinement-loop/scripts/tests/test_workflow_start_entry.py

Hermetic behavioral tests for `workflow_start_entry.py` (Issue #2311).

These tests exercise `workflow_start_entry.run()` with an injected fake
capability-preflight producer and an injected fake inner-preflight
invoker (dependency injection, matching this repo's existing
`FileBackedFakeGitHubEntryTransport` pattern in `root_entry_router.py` --
not internal monkeypatching of module internals). No live GitHub API call
and no live `uv`/`gh` subprocess is made by any test in this module; the
inner-preflight invoker fake stands in for both `run_refinement_preflight.py`
and any GitHub mutation it might otherwise perform, so proving it was
called zero times on the blocked path also proves zero GitHub mutation
occurred on that path (AC3's "no downstream GitHub mutation" claim).
"""

from __future__ import annotations

import importlib.util as _importlib_util
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import workflow_start_entry as wse  # noqa: E402


def _load_trusted_slug() -> str:
    """Trusted slug from the single identity source (config/repository_identity.v1.json)."""
    import importlib.util as _ilu

    for parent in Path(__file__).resolve().parents:
        module_path = parent / "scripts" / "agent-guards" / "repo_identity.py"
        if module_path.is_file():
            spec = _ilu.spec_from_file_location("_repo_identity_for_tests", module_path)
            module = _ilu.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.load_trusted_repo_slug()
    raise RuntimeError("repo_identity.py not found")


_REPO = _load_trusted_slug()

# PR #2320 review P0-2 item 3: import the REAL producer module
# (`scripts/agent-ops/workflow_capability_preflight.py`) via its file path
# -- `scripts/claude-gpt` is not an importable package and the module's
# on-disk name is not a valid dotted identifier prefix collision target, so
# `importlib.util` is used instead of a `sys.path` + plain `import`.
_PRODUCER_PATH = _SCRIPTS_DIR.parents[3] / "scripts" / "agent-ops" / "workflow_capability_preflight.py"
_producer_spec = _importlib_util.spec_from_file_location(
    "claude_gpt_workflow_capability_preflight", _PRODUCER_PATH
)
assert _producer_spec is not None and _producer_spec.loader is not None
claude_gpt_workflow_capability_preflight = _importlib_util.module_from_spec(_producer_spec)
sys.modules["claude_gpt_workflow_capability_preflight"] = claude_gpt_workflow_capability_preflight
_producer_spec.loader.exec_module(claude_gpt_workflow_capability_preflight)

_VALID_PLANNED_OPERATIONS_JSON = (
    '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
    '"operation": "issue_read", "requires_mutation": false}]'
)


def _make_recording_producer(decision: str, checks: dict | None = None, reasons: list | None = None):
    calls: list[dict] = []

    def _producer(**kwargs):
        calls.append(kwargs)
        return {"decision": decision, "checks": checks or {}, "reasons": reasons or []}

    return _producer, calls


def _make_recording_inner(returncode: int = 0):
    calls: list[dict] = []

    def _inner(**kwargs):
        calls.append(kwargs)
        return returncode

    return _inner, calls


def _failing_inner(**kwargs):
    raise AssertionError("inner preflight (and therefore any GitHub mutation) must not be invoked")


# ---------------------------------------------------------------------------
# AC3: blocked decision never invokes the inner preflight / GitHub mutation.
# ---------------------------------------------------------------------------


def test_workflow_start_blocked_does_not_invoke_inner_preflight():
    producer, producer_calls = _make_recording_producer("blocked", reasons=["no_trusted_uv"])

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert len(producer_calls) == 1
    assert result["inner_preflight_invoked"] is False
    assert result["status"] == "blocked"
    assert exit_code != 0


def test_workflow_start_blocked_performs_no_github_mutation():
    """AC3: the inner-preflight invoker fake is the sole place a GitHub
    mutation could occur in this module's control flow; a `blocked`
    decision must never reach it."""
    producer, producer_calls = _make_recording_producer("blocked", reasons=["github_auth_failed"])

    result, _exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert len(producer_calls) == 1
    assert result["inner_preflight_invoked"] is False


# ---------------------------------------------------------------------------
# AC4: ready / degraded invoke the inner preflight exactly once.
# ---------------------------------------------------------------------------


def test_workflow_start_ready_invokes_inner_once():
    producer, producer_calls = _make_recording_producer("ready")
    inner, inner_calls = _make_recording_inner(returncode=0)

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=inner,
    )

    assert len(producer_calls) == 1
    assert len(inner_calls) == 1
    assert result["inner_preflight_invoked"] is True
    assert result["status"] == "ready"
    assert exit_code == 0


def test_workflow_start_blocks_declared_spark_directive_before_producer_call():
    """Issue #2651: GPT-5.3-Codex-Spark delegation is retired -- an
    explicit `spark_mode` directive (any value, any `spark_fallback`) now
    deterministically retires to `blocked` WITHOUT ever calling the
    producer or the inner preflight (there is no more `degraded`/
    fallback_only continuation path)."""
    producer, producer_calls = _make_recording_producer(
        "degraded", checks={"spark": {"status": "fallback_only"}}
    )
    inner, inner_calls = _make_recording_inner(returncode=0)

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=inner,
    )

    assert producer_calls == []
    assert inner_calls == []
    assert result["inner_preflight_invoked"] is False
    assert result["decision"] == "blocked"
    assert result["reasons"] == ["spark_delegation_retired"]
    assert exit_code == 2


# ---------------------------------------------------------------------------
# AC5: exact caller-declared planned_operations pass-through when Spark is
# not requested (spark_mode=None); missing planned_operations fails closed
# BEFORE the producer is called.
# ---------------------------------------------------------------------------


def test_workflow_start_passes_exact_planned_operations_when_spark_not_requested():
    producer, producer_calls = _make_recording_producer("ready")
    inner, inner_calls = _make_recording_inner(returncode=0)
    planned_operations_json = (
        '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
        '"operation": "issue_comment", "requires_mutation": false}, '
        '{"phase": "step0g_contract_update", "actor_role": "contract-repair", '
        '"operation": "issue_edit", "requires_mutation": true}]'
    )

    wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=planned_operations_json,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=inner,
    )

    assert len(producer_calls) == 1
    call = producer_calls[0]
    assert call["repo"] == _REPO
    # Issue #2651: the producer no longer receives spark_mode/spark_fallback
    # kwargs at all (root_entry_router.capability_preflight_result()'s
    # signature dropped them).
    assert "spark_mode" not in call
    assert "spark_fallback" not in call
    assert call["planned_operations"] == [
        {
            "phase": "workflow_start",
            "actor_role": "issue-refinement-loop",
            "operation": "issue_comment",
            "requires_mutation": False,
        },
        {
            "phase": "step0g_contract_update",
            "actor_role": "contract-repair",
            "operation": "issue_edit",
            "requires_mutation": True,
        },
    ]
    assert len(inner_calls) == 1


@pytest.mark.parametrize(
    "planned_operations_json",
    [
        None,
        "",
        "not json",
        "{}",
        "[]",
        '[{"phase": "p"}]',
        '["not-an-object"]',
        # PR #2320 review P0-2 item 1: schema now requires a non-empty
        # 'operation' string and a strictly-bool 'requires_mutation'.
        '[{"phase": "p", "actor_role": "r", "requires_mutation": true}]',
        '[{"phase": "p", "actor_role": "r", "operation": "", "requires_mutation": true}]',
        '[{"phase": "p", "actor_role": "r", "operation": 7, "requires_mutation": true}]',
        '[{"phase": "p", "actor_role": "r", "operation": "issue_comment"}]',
        '[{"phase": "p", "actor_role": "r", "operation": "issue_comment", "requires_mutation": "yes"}]',
        '[{"phase": "p", "actor_role": "r", "operation": "issue_comment", "requires_mutation": 1}]',
    ],
)
def test_workflow_start_missing_planned_operations_fails_closed(planned_operations_json):
    def _unreachable_producer(**kwargs):
        raise AssertionError("producer must not be invoked when the caller request is malformed")

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=planned_operations_json,
        capability_preflight_result_fn=_unreachable_producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert result["status"] == "blocked"
    assert result["reason"].startswith("environment_failure:")
    assert result["inner_preflight_invoked"] is False
    assert exit_code != 0


# ---------------------------------------------------------------------------
# PR #2320 review P0-1 item 2: the prior fix_delta round's
# `planned_operations_omitted` exception (treating a genuinely OMITTED
# `--planned-operations-json` / `LOOP_PLANNED_OPERATIONS_JSON` as an
# implicit empty-but-valid operations set) is REMOVED. It papered over the
# real defect -- the caller's declared request could not reach this
# process through the canonical executor at all (P0-1 item 1, fixed via
# `skill_runtime_exec.py`'s `_sanitize_env()` allowlist) -- rather than
# fixing that reachability gap. The original Issue #2311 AC5 contract is
# restored: a caller-declared request that is missing OR malformed fails
# closed as `environment_failure` BEFORE the producer is ever invoked, with
# no special-cased "omitted means empty" bypass. The registry-shaped
# `--issue-number`/`--repo`-only CLI invocation from `command_registry.py`
# is exercised directly below to prove this at the `main()` layer, matching
# the real production invocation shape byte-for-byte.
# ---------------------------------------------------------------------------


def test_build_capability_request_no_longer_accepts_omitted_bypass():
    """`build_capability_request()` no longer has a
    `planned_operations_omitted` parameter at all -- a caller cannot bypass
    the fail-closed check by any means."""
    import inspect

    sig = inspect.signature(wse.build_capability_request)
    assert "planned_operations_omitted" not in sig.parameters
    with pytest.raises(wse.CapabilityRequestError):
        wse.build_capability_request(spark_mode=None, spark_fallback=None, planned_operations_json=None)


def test_run_no_longer_accepts_omitted_bypass():
    import inspect

    sig = inspect.signature(wse.run)
    assert "planned_operations_omitted" not in sig.parameters


def test_main_cli_with_only_issue_number_and_repo_fails_closed_without_env_request(monkeypatch):
    """Reproduces the real production registry-driven invocation shape
    (`--issue-number` / `--repo` only, matching `command_registry.py`'s
    bare `preflight.run` argv byte-for-byte) at the `main()` CLI-parsing
    layer, WITHOUT any of the three `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK`/
    `LOOP_PLANNED_OPERATIONS_JSON` env vars set (the canonical executor
    lane before a caller has declared any capability request). This must
    fail closed as `environment_failure` and the producer must never be
    invoked (requirement 2 of PR #2320 review's minimum 6 verification
    cases) -- unlike the removed omission-bypass behavior above. `wse.run`'s
    real (non-monkeypatched) default `capability_preflight_result_fn` is
    used deliberately: `run()` raises/catches `CapabilityRequestError`
    BEFORE that default is ever called on this path, so no live subprocess
    is spawned even though the default is not faked out here."""
    for env_name in (
        "LOOP_SPARK_MODE",
        "LOOP_SPARK_FALLBACK",
        "LOOP_PLANNED_OPERATIONS_JSON",
    ):
        monkeypatch.delenv(env_name, raising=False)

    exit_code = wse.main(["--issue-number", "2311", "--repo", _REPO])

    assert exit_code != 0


# ---------------------------------------------------------------------------
# PR #2320 review P0-2 item 2: spark_mode/spark_fallback are restricted to a
# fixed enum, matching `workflow_capability_preflight.py`'s own
# `argparse(choices=...)`, and this validation applies to the env-var
# fallback path too (not just the CLI-flag `choices=` boundary).
# ---------------------------------------------------------------------------


def test_build_capability_request_rejects_invalid_spark_mode():
    with pytest.raises(wse.CapabilityRequestError):
        wse.build_capability_request(
            spark_mode="sonnet",
            spark_fallback=None,
            planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        )


def test_build_capability_request_rejects_invalid_spark_fallback():
    with pytest.raises(wse.CapabilityRequestError):
        wse.build_capability_request(
            spark_mode="required",
            spark_fallback="haiku",
            planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        )


def test_build_capability_request_accepts_valid_spark_enum_values():
    request = wse.build_capability_request(
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
    )
    assert request["spark_mode"] == "required"
    assert request["spark_fallback"] == "forbidden"


def test_main_cli_rejects_invalid_spark_mode_at_argparse_boundary():
    with pytest.raises(SystemExit):
        wse.main(
            [
                "--issue-number",
                "2311",
                "--repo",
                _REPO,
                "--spark-mode",
                "sonnet",
            ]
        )


def test_main_env_fallback_rejects_invalid_spark_mode(monkeypatch):
    """The `LOOP_SPARK_MODE` env-var fallback bypasses argparse's own
    `choices=` enforcement (it is read via `os.environ.get`, not parsed by
    argparse), so `build_capability_request()` must validate it explicitly
    -- this proves that validation actually runs on the env-var path, not
    just the CLI-flag path. As above, the real (non-monkeypatched) producer
    default is safe to leave wired in: the invalid `spark_mode` is rejected
    by `build_capability_request()` before the producer default is ever
    invoked."""
    monkeypatch.setenv("LOOP_SPARK_MODE", "sonnet")
    monkeypatch.setenv("LOOP_PLANNED_OPERATIONS_JSON", _VALID_PLANNED_OPERATIONS_JSON)
    monkeypatch.delenv("LOOP_SPARK_FALLBACK", raising=False)

    exit_code = wse.main(["--issue-number", "2311", "--repo", _REPO])

    assert exit_code != 0


# ---------------------------------------------------------------------------
# AC7: blocked reason/checks/reasons are preserved verbatim in the compact
# result (no boolean reduction on the Step 0 path).
# ---------------------------------------------------------------------------


def test_workflow_start_preserves_block_reason():
    producer, _calls = _make_recording_producer(
        "blocked",
        checks={"uv": {"status": "missing"}, "github": {"auth": False}},
        reasons=["uv_not_found", "github_auth_failed"],
    )

    result, _exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert result["checks"] == {"uv": {"status": "missing"}, "github": {"auth": False}}
    assert result["reasons"] == ["uv_not_found", "github_auth_failed"]
    assert result["reason"] == "capability_preflight_blocked"


# ---------------------------------------------------------------------------
# AC3: producer invocation failure / malformed result fails closed exactly
# like a `blocked` decision (root_entry_router.capability_preflight_result
# itself normalizes both into decision="blocked" -- this test proves
# workflow_start_entry.run() treats that normalized shape correctly and
# still never invokes the inner preflight).
# ---------------------------------------------------------------------------


def test_workflow_start_malformed_producer_result_fails_closed():
    def _malformed_producer(**kwargs):
        # Mirrors root_entry_router.capability_preflight_result()'s own
        # fail-closed normalization of a malformed/failed producer
        # invocation into a synthetic decision="blocked" result.
        return {
            "decision": "blocked",
            "checks": {},
            "reasons": ["producer_result_malformed:non_json_stdout"],
        }

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=_malformed_producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert result["status"] == "blocked"
    assert result["inner_preflight_invoked"] is False
    assert "producer_result_malformed:non_json_stdout" in result["reasons"]
    assert exit_code != 0


# ---------------------------------------------------------------------------
# PR #2320 review P0-2 item 3: cross-contract test proving a payload built
# by wrapper-side `build_capability_request()` -> JSON-serialized ->
# re-loaded by the REAL producer's own `_load_planned_operations()` (not a
# fake) normalizes identically. This is what a real subprocess boundary
# (`workflow_capability_preflight.py` invoked as a child process) actually
# receives, so this test proves the wrapper and the real producer agree on
# schema without requiring a live subprocess/E2E.
# ---------------------------------------------------------------------------


def test_wrapper_payload_is_accepted_by_real_producer_loader(tmp_path):
    import json

    producer_mod = sys.modules["claude_gpt_workflow_capability_preflight"]

    planned_operations_json = (
        '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
        '"operation": "issue_comment", "requires_mutation": false}, '
        '{"phase": "step0g_contract_update", "actor_role": "contract-repair", '
        '"operation": "issue_edit", "requires_mutation": true}]'
    )

    request = wse.build_capability_request(
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations_json=planned_operations_json,
    )

    payload_path = tmp_path / "planned-operations.json"
    payload_path.write_text(json.dumps(request["planned_operations"]), encoding="utf-8")

    reloaded = producer_mod._load_planned_operations(str(payload_path))

    assert reloaded == [
        {
            "phase": "workflow_start",
            "actor_role": "issue-refinement-loop",
            "operation": "issue_comment",
            "requires_mutation": False,
        },
        {
            "phase": "step0g_contract_update",
            "actor_role": "contract-repair",
            "operation": "issue_edit",
            "requires_mutation": True,
        },
    ]


# ---------------------------------------------------------------------------
# AC2: workflow_start_entry.py must not reimplement the capability-preflight
# producer invocation itself -- it must call
# root_entry_router.capability_preflight_result (imported, not duplicated).
# ---------------------------------------------------------------------------


def test_workflow_start_uses_root_entry_router_capability_preflight_result():
    import root_entry_router as rer

    assert wse.root_entry_router is rer
    # The production default callable wired into run() is exactly
    # root_entry_router.capability_preflight_result -- not a local
    # reimplementation.
    import inspect

    sig = inspect.signature(wse.run)
    default = sig.parameters["capability_preflight_result_fn"].default
    assert default is rer.capability_preflight_result
