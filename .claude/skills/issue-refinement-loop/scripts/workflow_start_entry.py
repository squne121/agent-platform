#!/usr/bin/env python3
"""
workflow_start_entry.py

Thin production carrier for the canonical bare `preflight.run` command
(Issue #2311 AC1/AC2). This is the ONLY script directly reachable from
`command_registry.py`'s bare `preflight.run` entry (sibling anchor-comment
profiles -- `preflight.run.with_anchor` / `.with_human_context` /
`.with_agent_report` / `.fixture` / `.fixture.with_human_context` --
continue to first-hop into `run_refinement_preflight.py` directly and are
unaffected by this module; Issue #2311 is scoped to the bare command only).

Issue #2651: GPT-5.3-Codex-Spark delegation is retired repository-wide. A
caller-declared `spark_mode` (`required`/`preferred`) is still validated
against its fixed enum (step 1a below) but now always yields a
deterministic `blocked` result (`reasons: ["spark_delegation_retired"]`)
BEFORE the capability-preflight producer is ever invoked -- never a live
eligible/fallback_only judgment, and never a silent fallback to a
different model/agent. `spark_mode=None` (the ordinary case) is
unaffected.

Within a SINGLE continuous invocation, this module:

  1. Assembles the caller-declared, invocation-scoped capability request
     (`spark_mode` / `spark_fallback` / `planned_operations`, each operation
     carrying `phase` / `actor_role` / `operation` / `requires_mutation` --
     matching `workflow_capability_preflight.py`'s own producer-side
     `_load_planned_operations()` schema byte-for-byte, PR #2320 review
     P0-2). This request is NOT a static global superset of every possible
     workflow_operation -- it is whatever THIS invocation's caller
     explicitly declares (via CLI flags, or their `LOOP_SPARK_MODE` /
     `LOOP_SPARK_FALLBACK` / `LOOP_PLANNED_OPERATIONS_JSON`
     environment-variable fallback, since the canonical `preflight.run`
     registry argv itself only carries `--issue-number` / `--repo` -- Issue
     #2311 P0-6 / AC1 keep that argv shape byte-for-byte otherwise
     unchanged; the caller instead exports these three env vars scoped to
     THIS ONE invocation only -- never as a persistent/exported shell
     setting -- immediately before running the canonical `preflight.run`
     executor, per `SKILL.md`'s capability-request setup procedure, PR
     #2320 review P0-1 item 3). `scripts/agent-guards/skill_runtime_exec.py`
     `_sanitize_env()` allowlists exactly these three env var names for the
     `preflight.run` command id so this env-var transport actually reaches
     this process when launched via the canonical executor (PR #2320
     review P0-1 item 1 -- previously these three names were silently
     stripped by the executor's allowlist, so a canonical
     `skill_runtime_exec.py --command-id preflight.run ...` invocation
     always observed `None`/`None`/absent regardless of what the caller
     intended). A missing or malformed request -- including a genuinely
     OMITTED `planned_operations` declaration -- fails closed as
     `environment_failure` BEFORE the producer is ever invoked (AC5, PR
     #2320 review P0-1 item 2: the "missing declaration is treated as an
     empty-but-valid set" exception introduced by a prior fix_delta round
     is REMOVED -- it papered over the fact that the request could not
     reach this process at all, rather than fixing that reachability gap).
     This is independent of, and checked earlier than, the AC3
     producer-response malformed case below.
  1a. `spark_mode` is restricted to `required` / `preferred` and
     `spark_fallback` to `forbidden` / `allowed` (`workflow_capability_
     preflight.py`'s own fixed enum, PR #2320 review P0-2 item 2). The CLI
     flags reject any other value at the `argparse` boundary
     (`choices=...`); the `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK`
     environment-variable fallback is validated by the SAME
     `_validate_spark_mode`/`_validate_spark_fallback` helpers explicitly
     (env-var input bypasses `argparse`'s own `choices` enforcement, so it
     cannot rely on that alone).
  2. Calls `root_entry_router.capability_preflight_result()` (imported, not
     reimplemented -- AC2) exactly once to run the workflow capability
     preflight producer.
  3. If the producer's `decision` is `blocked`, OR the producer invocation
     itself failed / returned a malformed result (`capability_preflight_
     result()` already normalizes both of those into a synthetic
     `decision: "blocked"` result -- see that function's docstring), this
     module returns a compact blocked result WITHOUT ever invoking
     `run_refinement_preflight.py` (AC3) -- `checks` / `reasons` from the
     producer are preserved verbatim (AC7).
  4. If `decision` is `ready` or `degraded`, this module invokes
     `run_refinement_preflight.py` exactly once, via the SAME trusted
     interpreter this process is already running under (`sys.executable`).
     This process was itself launched as `uv run python3
     workflow_start_entry.py ...` by the trusted `skill_runtime_exec.py`
     dispatcher, which already resolved a trusted `uv`/`python3` pair
     before spawning this process -- so `sys.executable` already IS the
     interpreter produced by that canonical trusted-`uv` resolution.
     Re-deriving a second, independent trusted-`uv` resolver inside this
     module would duplicate `skill_runtime_exec.py`'s existing trust logic
     rather than reuse it (Issue #2311 P0-6: "canonical resolver は既に
     信頼済み uv の絶対パスを返せるので、新しい resolver は不要") -- so this
     module deliberately reuses `sys.executable` instead of shelling out to
     `uv` a second time (AC4).

This module is a thin synchronous wrapper: no persistent state, no
authorization token, no ledger, no digest/receipt. Step 0's capability
preflight (this module) and Step 5's `run_root_transition` capability
preflight are independent fresh calls -- the result of this module is
NEVER reused as Step 5 implementation-entry authorization (see
`SKILL.md`'s Step 0 section).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import root_entry_router  # noqa: E402  (sys.path must be prepared first)

# Issue #2323: the blocked / caller-capability-request-malformed paths below
# reuse the SAME compact stdout line-grammar renderer
# (`STATUS:`/`NEXT_ACTION:`/`MUST_READ:`/`COMMANDS_JSON:`/`BLOCKERS:`/
# `ARTIFACT:` etc.) that `run_refinement_preflight.py` already uses for its
# own ready/degraded stdout, instead of a separate raw-JSON shape. This is a
# stdout-grammar unification only -- `refinement_preflight_result_v1.schema.json`
# (the JSON artifact schema) itself is not produced or validated here.
from run_refinement_preflight import _build_compact_stdout  # noqa: E402

SCHEMA_VERSION = "WORKFLOW_START_ENTRY_RESULT_V1"

_REQUIRED_OPERATION_KEYS = ("phase", "actor_role", "operation", "requires_mutation")

_ENV_SPARK_MODE = "LOOP_SPARK_MODE"
_ENV_SPARK_FALLBACK = "LOOP_SPARK_FALLBACK"
_ENV_PLANNED_OPERATIONS_JSON = "LOOP_PLANNED_OPERATIONS_JSON"

# Fixed enums, matching `workflow_capability_preflight.py`'s own
# `argparse` `choices=[...]` for `--spark-mode`/`--spark-fallback`
# byte-for-byte (Issue #2311 fix_delta / PR #2320 review P0-2 item 2).
_SPARK_MODE_CHOICES = ("required", "preferred")
_SPARK_FALLBACK_CHOICES = ("forbidden", "allowed")


class CapabilityRequestError(ValueError):
    """Raised internally when the caller-declared capability request is
    missing or malformed (AC5). Never escapes this module -- ``run()``
    catches it and converts it into a compact ``environment_failure``
    blocked result before the producer is ever invoked."""


def _validate_spark_mode(value: Optional[str]) -> Optional[str]:
    """Validate `spark_mode` against the fixed enum (PR #2320 review P0-2
    item 2). `None` (Spark not requested at all) always passes through --
    only a NON-None value outside the enum is rejected. Applied uniformly
    to both the `--spark-mode` CLI flag and the `LOOP_SPARK_MODE`
    environment-variable fallback (the latter bypasses `argparse`'s own
    `choices=` enforcement, so it needs this same check explicitly)."""
    if value is None:
        return None
    if value not in _SPARK_MODE_CHOICES:
        raise CapabilityRequestError(f"spark_mode_invalid:{value}")
    return value


def _validate_spark_fallback(value: Optional[str]) -> Optional[str]:
    """Validate `spark_fallback` against the fixed enum (PR #2320 review
    P0-2 item 2). Same `None`-passthrough / CLI+env uniform-validation
    contract as `_validate_spark_mode` above."""
    if value is None:
        return None
    if value not in _SPARK_FALLBACK_CHOICES:
        raise CapabilityRequestError(f"spark_fallback_invalid:{value}")
    return value


def _parse_planned_operations(raw: Optional[str]) -> list[dict]:
    """Parse and validate the caller-declared, invocation-scoped
    `planned_operations` JSON array. Raises `CapabilityRequestError` if the
    value is missing, non-JSON, not a non-empty list, or if any entry is
    missing one of `phase` / `actor_role` / `operation` / `requires_mutation`,
    has a non-string/empty `operation`, or has a non-bool `requires_mutation`
    (AC5; schema aligned with `workflow_capability_preflight.py`'s own
    `_load_planned_operations()` -- PR #2320 review P0-2 item 1). This
    function never invents a default -- an absent declaration is a caller
    error, not treated as an empty-but-valid set: a workflow-start
    invocation that intends zero downstream mutation must say so with an
    explicit empty-operations declaration; the caller MUST declare its
    planned operations (PR #2320 review P0-1 item 2 -- a prior fix_delta
    round's `planned_operations_omitted` exception, which treated a
    genuinely absent declaration as an implicit empty set, is REMOVED. That
    exception existed only because the caller's real declaration could not
    reach this process through the canonical executor at all; now that the
    executor's env allowlist carries it through (see module docstring),
    the original fail-closed contract is restored without loss of
    reachability)."""
    if raw is None or not raw.strip():
        raise CapabilityRequestError("planned_operations_missing")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CapabilityRequestError("planned_operations_not_json") from exc
    if not isinstance(data, list) or not data:
        raise CapabilityRequestError("planned_operations_not_nonempty_list")
    for operation in data:
        if not isinstance(operation, dict):
            raise CapabilityRequestError("planned_operations_entry_not_object")
        missing = [key for key in _REQUIRED_OPERATION_KEYS if key not in operation]
        if missing:
            raise CapabilityRequestError(f"planned_operations_entry_missing_keys:{','.join(missing)}")
        if not isinstance(operation["operation"], str) or not operation["operation"]:
            raise CapabilityRequestError("planned_operations_entry_operation_not_nonempty_string")
        if not isinstance(operation["requires_mutation"], bool):
            raise CapabilityRequestError("planned_operations_entry_requires_mutation_not_bool")
    return data


def build_capability_request(
    *,
    spark_mode: Optional[str],
    spark_fallback: Optional[str],
    planned_operations_json: Optional[str],
) -> dict[str, Any]:
    """Assemble the caller-declared capability request. Raises
    `CapabilityRequestError` if `planned_operations_json` is missing or
    malformed (AC5 -- fail-closed, no omission exception; PR #2320 review
    P0-1 item 2), or if `spark_mode` / `spark_fallback` are set to a value
    outside their fixed enum (PR #2320 review P0-2 item 2). `spark_mode` /
    `spark_fallback` being `None` is always valid (a caller that never
    needs Spark may omit both)."""
    planned_operations = _parse_planned_operations(planned_operations_json)
    spark_mode = _validate_spark_mode(spark_mode)
    spark_fallback = _validate_spark_fallback(spark_fallback)
    return {
        "spark_mode": spark_mode,
        "spark_fallback": spark_fallback,
        "planned_operations": planned_operations,
    }


def _compact_result(
    *,
    status: str,
    reason: Optional[str],
    checks: dict,
    reasons: list,
    decision: Optional[str],
    inner_preflight_invoked: bool,
    next_action: Optional[str] = None,
    blockers: Optional[list] = None,
) -> dict[str, Any]:
    """Build this module's own blocked/malformed result dict.

    Issue #2323: in addition to the pre-existing `schema`/`status`/`reason`/
    `decision`/`checks`/`reasons`/`inner_preflight_invoked` keys (kept for
    `run()`'s return-value contract, consumed directly by tests), this dict
    also carries `next_action` / `blockers` -- the minimal key set
    `run_refinement_preflight.py::_build_compact_stdout()` needs to render
    the SAME compact stdout line grammar
    (`STATUS:`/`NEXT_ACTION:`/`BLOCKERS:` etc.) that the ready/degraded path
    already emits, instead of this module's previous raw-JSON stdout shape.
    `status` here reuses an existing `refinement_preflight_result_v1.schema
    .json` `status` enum value (`"blocked"`) already used before this Issue.
    `blockers` reuses the existing typed `reasons` values verbatim (AC3) --
    no new `reason_code` enum value is introduced.
    """
    return {
        "schema": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "decision": decision,
        "checks": checks,
        "reasons": reasons,
        "inner_preflight_invoked": inner_preflight_invoked,
        "next_action": next_action,
        "blockers": blockers if blockers is not None else [],
    }


def _default_invoke_inner_preflight(*, issue_number: int, repo: str) -> int:
    """Invoke `run_refinement_preflight.py` exactly once via the trusted
    interpreter this process is already running under (AC4 -- see module
    docstring). Inherits stdio so the inner script's own
    `refinement_preflight_result/v1` stdout contract passes through
    unmodified to this process's stdout/stderr and its exit code becomes
    this process's exit code."""
    inner_script = _SCRIPT_DIR / "run_refinement_preflight.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(inner_script),
            "--issue-number",
            str(issue_number),
            "--repo",
            repo,
        ],
        check=False,
    )
    return proc.returncode


def _validate_single_line_reasons(value: object) -> Optional[list[str]]:
    """Validate that `value` is a list of non-empty, single-line strings
    (Issue #2323 fix_delta P1: PR #2328 review
    source repository pull 2328 review).
    Returns the validated list, or `None` if `value` fails validation (not a
    list, or any element is not a non-empty single-line string). A "single
    line" element is one where `item.splitlines(keepends=True) == [item]`
    (rejects embedded `\n`, `\r`, and other Unicode line-boundary
    characters that `str.splitlines()` recognizes, e.g. `\u2028`), so a
    malformed producer `reasons` value can never forge additional
    `STATUS:`/`NEXT_ACTION:` lines in the compact stdout grammar."""
    if not isinstance(value, list):
        return None
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item.splitlines(keepends=True) != [item]
        ):
            return None
    return value


def run(
    *,
    issue_number: int,
    repo: str,
    spark_mode: Optional[str],
    spark_fallback: Optional[str],
    planned_operations_json: Optional[str],
    capability_preflight_result_fn: Callable[..., dict] = root_entry_router.capability_preflight_result,
    invoke_inner_preflight_fn: Callable[..., int] = _default_invoke_inner_preflight,
) -> tuple[dict[str, Any], int]:
    """Run the full workflow-start gate once. Returns `(result_dict,
    exit_code)`. `capability_preflight_result_fn` / `invoke_inner_preflight_fn`
    are injectable for hermetic fake-transport tests (AC9) -- production
    callers use the defaults (`root_entry_router.capability_preflight_result`
    and a `sys.executable`-based inner invocation)."""
    try:
        capability_request = build_capability_request(
            spark_mode=spark_mode,
            spark_fallback=spark_fallback,
            planned_operations_json=planned_operations_json,
        )
    except CapabilityRequestError as exc:
        # AC5: caller-side missing/malformed request. Fail closed WITHOUT
        # ever calling the producer.
        # Issue #2323 AC1/AC3: `blockers` carries the existing typed
        # `caller_capability_request_missing_or_malformed` reason verbatim
        # (no `"environment_failure:"` free-text prefix a consumer would
        # need to parse -- the specific exception detail is still preserved
        # in `reason` for diagnostics, but stdout's `BLOCKERS:` line is the
        # canonical typed signal now).
        result = _compact_result(
            status="blocked",
            reason=f"environment_failure:{exc}",
            checks={},
            reasons=["caller_capability_request_missing_or_malformed"],
            decision=None,
            inner_preflight_invoked=False,
            next_action="human_judgment_required",
            blockers=["caller_capability_request_missing_or_malformed"],
        )
        return result, 2

    # Issue #2651: GPT-5.3-Codex-Spark delegation is retired. When the
    # caller's capability request explicitly declares a `spark_mode`
    # (`required`/`preferred`), this module returns a deterministic
    # retired-blocked result WITHOUT ever invoking the producer (which no
    # longer accepts `spark_mode`/`spark_fallback` at all --
    # `root_entry_router.capability_preflight_result()`'s signature dropped
    # them in this same Issue, since that function has no external pinned
    # caller outside Allowed Paths). `spark_mode`/`spark_fallback` remain
    # accepted keyword-only parameters on THIS function -- unlike
    # `capability_preflight_result()` -- because a real external caller
    # outside this Issue's Allowed Paths
    # (runtime smoke test, not migrated)
    # already pins `wse.run(spark_mode=None, spark_fallback=None, ...)`; it
    # always passes `None` for both, so this retired branch is never
    # reached by that caller and its behavior is unaffected.
    if capability_request["spark_mode"] is not None:
        result = _compact_result(
            status="blocked",
            reason="spark_delegation_retired",
            checks={},
            reasons=["spark_delegation_retired"],
            decision="blocked",
            inner_preflight_invoked=False,
            next_action="human_judgment_required",
            blockers=["spark_delegation_retired"],
        )
        return result, 2

    # AC2: call the producer exactly once.
    producer_result = capability_preflight_result_fn(
        repo=repo,
        planned_operations=capability_request["planned_operations"],
    )
    decision = producer_result.get("decision")
    checks = producer_result.get("checks", {})
    reasons = producer_result.get("reasons", [])

    validated_reasons = _validate_single_line_reasons(reasons)
    if validated_reasons is None:
        malformed_reason = "producer_result_malformed:invalid_reasons"
        result = _compact_result(
            status="blocked",
            reason=malformed_reason,
            checks={},
            reasons=[malformed_reason],
            decision="blocked",
            inner_preflight_invoked=False,
            next_action="fix_environment",
            blockers=[malformed_reason],
        )
        return result, 2
    reasons = validated_reasons

    if decision not in ("ready", "degraded"):
        # AC3/AC7: `blocked` decision, or a producer invocation
        # failure/malformed result already normalized to `decision:
        # "blocked"` by `capability_preflight_result_fn`. Either way,
        # `run_refinement_preflight.py` is NEVER invoked on this path, and
        # the producer's own `checks`/`reasons` are preserved verbatim.
        # Issue #2323 AC2: `blockers` reuses the producer's own typed
        # `reasons` list verbatim (falling back to a single
        # `capability_preflight_<decision>` marker only when the producer
        # returned no typed reasons at all), so the compact stdout grammar's
        # `BLOCKERS:` line stays consumer-parseable without a free-text
        # prefix.
        result = _compact_result(
            status="blocked",
            reason=f"capability_preflight_{decision or 'unknown'}",
            checks=checks,
            reasons=reasons,
            decision=decision,
            inner_preflight_invoked=False,
            next_action="human_judgment_required",
            blockers=list(reasons) if reasons else [f"capability_preflight_{decision or 'unknown'}"],
        )
        return result, 2

    # AC4: ready/degraded -- invoke run_refinement_preflight.py exactly
    # once via the trusted interpreter.
    inner_returncode = invoke_inner_preflight_fn(issue_number=issue_number, repo=repo)
    result = _compact_result(
        status="ready" if inner_returncode == 0 else "inner_preflight_failed",
        reason=None,
        checks=checks,
        reasons=reasons,
        decision=decision,
        inner_preflight_invoked=True,
    )
    return result, inner_returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Workflow-start capability preflight gate. First-hop production "
            "carrier for the canonical bare `preflight.run` command "
            "(Issue #2311)."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--repo", required=True)
    # `choices` rejects any non-enum value supplied via the CLI flag at the
    # `argparse` boundary itself (PR #2320 review P0-2 item 2). `default=None`
    # remains valid even though it is not itself listed in `choices`
    # (argparse never validates the default value, only user-supplied
    # values), matching `workflow_capability_preflight.py`'s own
    # `--spark-mode`/`--spark-fallback` argparse definitions.
    parser.add_argument("--spark-mode", default=None, choices=list(_SPARK_MODE_CHOICES))
    parser.add_argument("--spark-fallback", default=None, choices=list(_SPARK_FALLBACK_CHOICES))
    parser.add_argument("--planned-operations-json", default=None)
    args = parser.parse_args(argv)

    # `LOOP_SPARK_MODE` / `LOOP_SPARK_FALLBACK` / `LOOP_PLANNED_OPERATIONS_JSON`
    # are this invocation's ONLY way to carry a capability request through
    # the canonical bare `preflight.run` registry argv, which itself only
    # ever carries `--issue-number`/`--repo` (Issue #2311 P0-6/AC1 keeps
    # that argv shape byte-for-byte unchanged). The caller is expected to
    # export these three env vars scoped to this ONE invocation only (never
    # as a persistent/exported shell setting -- see `SKILL.md`'s capability
    # request setup procedure, PR #2320 review P0-1 item 3), and
    # `scripts/agent-guards/skill_runtime_exec.py`'s `_sanitize_env()`
    # allowlists exactly these three names for the `preflight.run` command
    # id so a canonical `skill_runtime_exec.py --command-id preflight.run
    # ...` invocation actually carries them through to this process (PR
    # #2320 review P0-1 item 1).
    #
    # A genuinely OMITTED declaration (neither the CLI flag nor the env var
    # supplied) fails closed exactly like an explicitly malformed one --
    # `_parse_planned_operations`/`run()` raise/return `environment_failure`
    # for both cases with no special-cased bypass (PR #2320 review P0-1
    # item 2: the prior fix_delta round's `planned_operations_omitted`
    # exception is removed).
    spark_mode = args.spark_mode if args.spark_mode is not None else os.environ.get(_ENV_SPARK_MODE)
    spark_fallback = (
        args.spark_fallback if args.spark_fallback is not None else os.environ.get(_ENV_SPARK_FALLBACK)
    )
    planned_operations_json = (
        args.planned_operations_json
        if args.planned_operations_json is not None
        else os.environ.get(_ENV_PLANNED_OPERATIONS_JSON)
    )

    result, exit_code = run(
        issue_number=args.issue_number,
        repo=args.repo,
        spark_mode=spark_mode,
        spark_fallback=spark_fallback,
        planned_operations_json=planned_operations_json,
    )
    if not result.get("inner_preflight_invoked"):
        # The inner preflight's own stdout contract (`refinement_preflight_
        # result/v1`) already passed through unmodified when it WAS
        # invoked; only print this module's own compact result on the
        # blocked path, where there is no inner stdout to preserve.
        # Issue #2323 AC1/AC2: render the SAME compact stdout line grammar
        # (`STATUS:`/`NEXT_ACTION:`/`BLOCKERS:` etc.) the ready/degraded
        # path already emits via `run_refinement_preflight.py`, instead of
        # this module's previous raw `{"schema": "WORKFLOW_START_ENTRY_
        # RESULT_V1", ...}` JSON dict.
        print(_build_compact_stdout(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
