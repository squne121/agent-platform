"""scripts/agent-ops/workflow_capability_preflight.py

Issue #2273: `--workflow-profile issue-to-impl` workflow capability
preflight. Produces the `CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1` structured
result described in the Issue's `## Result Schema` section.

This module judges, SEPARATELY:

  - trusted `uv` availability (delegated to
    `scripts/agent-guards/trusted_runtime_capabilities.py`, which itself
    delegates to the canonical `skill_runtime_exec` resolver -- no second
    resolver is introduced here)
  - Spark delegation route capability: RETIRED (Issue #2651).
    `spark_mode`/`spark_fallback` remain accepted keyword-only arguments on
    `assess()` for byte-for-byte call-signature compatibility with existing
    ordinary callers that pass `spark_mode=None`, but any NON-None `spark_mode` now produces a deterministic
    `"retired"` verdict -- never a live binary/auth-based
    eligible/fallback_only judgment, and never a silent fallback to a
    different model/agent.
  - GitHub READ capability (`gh auth status` + a repository read probe)
  - GitHub WRITE (mutation) capability, evaluated PER planned operation
    (`phase`/`actor_role`/`operation`) against a small, invocation-scoped
    `planned_operations` list the caller supplies -- never a persistent
    actor/operation authorization registry (Issue #2223 Owner Decision:
    no second capability registry)

`permission` is always reported as `"unverified"`: this module never
proves GitHub server-side write permission -- that is established later by
a mutation-capable actor's pre/post live readback (Issue #2223 model).

This module performs NO GitHub state mutation of its own (AC12): every
GitHub call it makes is a read (`gh auth status`, `gh repo view`).

Exit code contract: a well-formed assessment (including `decision:
blocked`) exits 0. Non-zero exit codes are reserved for invalid input /
internal errors, so a `set -e` caller does not lose the JSON payload.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"

if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import trusted_runtime_capabilities as trusted_uv_mod
from repo_identity import RepositoryIdentityError, is_trusted_repo, load_trusted_repo_slug

# Issue #2340 AC2/AC3: the `controlled_github_read` actor-scoped probe below
# needs the SAME ambient-env sanitize-key set as
# `controlled_skill_mutation_exec.py::_build_metadata_sanitized_env()`, so
# this preflight's `controlled_github_read` verdict reflects the EXACT
# credential/host context the downstream issue-editor / contract-update
# lane executes its GitHub read/write subprocess calls under.
#
# Issue #2340 fix_delta P0-1 (PR #2357 review, 2026-08-27): this list
# strips execution/log-hygiene NOISE only, not the GitHub credential
# carrier. `GH_CONFIG_DIR` / `GH_TOKEN` / `GITHUB_TOKEN` are deliberately
# left intact -- this probe must observe the SAME credential availability
# the controlled executor's write helpers do post-fix, otherwise a
# `controlled_github_read: ready` verdict here would not actually predict
# whether the downstream write can authenticate (see
# `controlled_skill_mutation_exec._METADATA_ENV_NOISE_STRIP_KEYS`).
#
# This list is intentionally DUPLICATED here (not imported), matching the
# same pattern `.claude/skills/edit-issue/scripts/edit_issue_txn.py`
# already uses for the identical reason (see that module's
# `_GH_ENV_SANITIZE_KEYS` comment): `workflow_capability_preflight.py` is
# copied verbatim -- WITHOUT `controlled_skill_mutation_policy.py` -- into
# fixture harnesses that exercise the real
# `skill_runtime_exec.py -> workflow_start_entry.py ->
# root_entry_router.capability_preflight_result() ->
# workflow_capability_preflight.py` chain (see
# `.claude/skills/issue-refinement-loop/scripts/tests/
# test_workflow_start_entry_canonical_executor.py`). A top-level import of
# `controlled_skill_mutation_policy` here would make this module
# unimportable inside that fixture even though it only exercises the plain
# GitHub-read capability path, not any controlled-mutation policy. A parity
# test (`scripts/agent-guards/tests/
# test_controlled_skill_mutation_exec_env_parity.py`) asserts this list stays
# in sync with the canonical source.
_ENV_SANITIZE_KEYS = (
    "PUBLISH_ARTIFACT_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "GH_EDITOR",
    "EDITOR",
    "VISUAL",
    "BROWSER",
    "GH_HOST",
    "GH_REPO",
    "GH_DEBUG",
    "DEBUG",
)

SCHEMA = "CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1"
SUPPORTED_PROFILES = ("issue-to-impl",)

DECISION_READY = "ready"
DECISION_DEGRADED = "degraded"
DECISION_BLOCKED = "blocked"

SPARK_NOT_REQUIRED = "not_required"
# Issue #2651: GPT-5.3-Codex-Spark delegation is retired. `SPARK_ELIGIBLE` /
# `SPARK_FALLBACK_ONLY` / `SPARK_UNAVAILABLE` (the live binary/auth-based
# judgment enum) are removed; any non-None `spark_mode` now maps
# deterministically to `SPARK_RETIRED` regardless of proxy binary/ChatGPT
# auth availability.
SPARK_RETIRED = "retired"

# Issue #2340 AC2: actor/execution-substrate-scoped capability status enum.
# Distinct from the overall `decision` enum (`ready`/`degraded`/`blocked`) --
# these are per-actor entries reported ALONGSIDE the overall verdict, not a
# replacement for it.
ACTOR_CAPABILITY_READY = "ready"
ACTOR_CAPABILITY_DEGRADED = "degraded"
ACTOR_CAPABILITY_UNAVAILABLE = "unavailable"

# GitHub operations that mutate state. Their native routes are NOT migrated
# into this repository yet (Stage 2), so no route may be reported available.
# Names in this set are always treated as mutation-requiring, whatever the
# caller declared for `requires_mutation` (fail closed).
_MUTATION_OPERATION_NAMES = frozenset(
    {
        "issue_comment",
        "issue_edit",
        "issue_create",
        "pr_comment",
        "pr_create",
        "pr_edit",
        "pr_update_branch",
    }
)

# Operations with an implemented route in this repository. Empty until the
# Stage 2 GitHub mutation implementation is migrated; a route is "available"
# only if it is listed here (Issue #9: no false-ready for unmigrated routes).
_KNOWN_OPERATION_ROUTES: frozenset[str] = frozenset()

DEFAULT_BUDGET_SECONDS = 30.0
_GITHUB_AUTH_PROBE_CAP_SECONDS = 30.0
_GITHUB_REPO_READ_PROBE_CAP_SECONDS = 30.0
_CONTROLLED_GITHUB_READ_PROBE_CAP_SECONDS = 15.0

PROBE_COMPLETED = "completed"
PROBE_NONZERO_EXIT = "nonzero_exit"
PROBE_TIMEOUT = "probe_timeout"
PROBE_DEADLINE_EXHAUSTED = "deadline_exhausted_before_spawn"
PROBE_SPAWN_ERROR = "spawn_error"
PROBE_MALFORMED_OUTPUT = "malformed_output"


@dataclass(frozen=True)
class ProbeOutcome:
    """Internal, typed result of one deadline-bound subprocess probe."""

    kind: str
    stdout: str = ""
    returncode: int | None = None
    error_name: str | None = None


def _local_deadline_ns() -> int:
    return time.monotonic_ns() + int(DEFAULT_BUDGET_SECONDS * 1_000_000_000)


def _run_probe_with_deadline(
    probe_name: str,
    argv: list[str],
    *,
    deadline_ns: int,
    cap_seconds: float,
    env: dict[str, str] | None = None,
) -> ProbeOutcome:
    """Run one probe within its share of the common absolute deadline.

    A deadline that has already passed is an operational result, not an
    invocation failure: no subprocess is started and the caller can preserve
    the probe name in its structured reason.
    """
    del probe_name  # retained in this small boundary's explicit call contract
    remaining_seconds = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
    if remaining_seconds <= 0:
        return ProbeOutcome(PROBE_DEADLINE_EXHAUSTED)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=min(cap_seconds, remaining_seconds),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ProbeOutcome(PROBE_TIMEOUT)
    except OSError as exc:
        return ProbeOutcome(PROBE_SPAWN_ERROR, error_name=exc.__class__.__name__)
    if proc.returncode != 0:
        return ProbeOutcome(PROBE_NONZERO_EXIT, stdout=proc.stdout, returncode=proc.returncode)
    return ProbeOutcome(PROBE_COMPLETED, stdout=proc.stdout, returncode=proc.returncode)


def _spark_status(spark_mode: str | None) -> str:
    """Judge Spark route status from a caller-declared directive
    (`spark_mode`) alone (Issue #2651: GPT-5.3-Codex-Spark delegation is
    retired). No live binary/auth probe is run for Spark any more -- a
    non-None `spark_mode` (`required`/`preferred`, regardless of
    `spark_fallback`) always maps to `SPARK_RETIRED`, never to a
    live-observed eligible/fallback_only judgment and never to a silent
    fallback."""
    if not spark_mode:
        return SPARK_NOT_REQUIRED
    return SPARK_RETIRED


def _github_auth_probe(deadline_ns: int) -> ProbeOutcome:
    return _run_probe_with_deadline(
        "github_auth",
        ["gh", "auth", "status", "--active", "--hostname", "github.com"],
        deadline_ns=deadline_ns,
        cap_seconds=_GITHUB_AUTH_PROBE_CAP_SECONDS,
    )


def _github_repo_read_probe(repo: str, deadline_ns: int) -> ProbeOutcome:
    return _run_probe_with_deadline(
        "github_repo_read",
        ["gh", "repo", "view", f"github.com/{repo}", "--json", "name"],
        deadline_ns=deadline_ns,
        cap_seconds=_GITHUB_REPO_READ_PROBE_CAP_SECONDS,
    )


# Compatibility adapters retained for direct diagnostic callers. Production
# assessment uses the typed probe functions above so every probe shares its
# caller-provided deadline.
def _github_auth_ok() -> bool:
    return _github_auth_probe(_local_deadline_ns()).kind == PROBE_COMPLETED


def _github_repo_read_ok(repo: str) -> bool:
    return _github_repo_read_probe(repo, _local_deadline_ns()).kind == PROBE_COMPLETED


def _sanitized_controlled_env() -> dict[str, str]:
    """Build the sanitized environment for the `controlled_github_read` probe
    below -- byte-for-byte the same policy
    `controlled_skill_mutation_exec.py::_build_metadata_sanitized_env()` uses
    (Issue #2340 AC1/AC2). Uses the locally duplicated `_ENV_SANITIZE_KEYS`
    (see the import-boundary comment above -- kept in sync by a parity
    test, not by a shared import)."""
    env = os.environ.copy()
    for key in _ENV_SANITIZE_KEYS:
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def _root_github_read_capability(github_auth: bool, github_repo_read: bool) -> dict:
    """Actor-scoped entry (Issue #2340 AC2): the root/main process's own
    `gh auth status` + `gh repo view` probe, ambient-env, unsanitized. This is
    the check root preflight has ALWAYS made -- kept here unchanged so its
    result can be compared side-by-side against `controlled_github_read`
    below (AC2/AC7 `same_identity` comparison input)."""
    if github_auth and github_repo_read:
        return {
            "status": ACTOR_CAPABILITY_READY,
            "reason_code": None,
            "fallback_route": None,
            "probe_execution_class": "root_shell_gh_repo_view",
        }
    reason_code = (
        "root_github_auth_unavailable" if not github_auth else "root_github_repo_read_failed"
    )
    return {
        "status": ACTOR_CAPABILITY_UNAVAILABLE,
        "reason_code": reason_code,
        "fallback_route": None,
        "probe_execution_class": "root_shell_gh_repo_view",
    }


def _controlled_github_read_probe(repo: str, deadline_ns: int) -> ProbeOutcome:
    return _run_probe_with_deadline(
        "controlled_github_read",
        ["gh", "api", "--hostname", "github.com", f"repos/{repo}", "--jq", "{name}"],
        deadline_ns=deadline_ns,
        cap_seconds=_CONTROLLED_GITHUB_READ_PROBE_CAP_SECONDS,
        env=_sanitized_controlled_env(),
    )


def _controlled_github_read_from_outcome(outcome: ProbeOutcome) -> dict:
    if outcome.kind == PROBE_COMPLETED:
        return {
            "status": ACTOR_CAPABILITY_READY,
            "reason_code": None,
            "fallback_route": None,
            "probe_execution_class": "controlled_gh_api_repo_read",
        }
    return {
        "status": ACTOR_CAPABILITY_UNAVAILABLE,
        "reason_code": "controlled_github_unavailable",
        "fallback_route": None,
        "probe_execution_class": "controlled_gh_api_repo_read",
    }


def _controlled_github_read_capability(repo: str) -> dict:
    """Compatibility adapter for callers outside the shared-deadline path."""
    return _controlled_github_read_from_outcome(
        _controlled_github_read_probe(repo, _local_deadline_ns())
    )


def _delegated_research_agy_capability() -> dict:
    """Actor-scoped entry (Issue #2340 AC2/AC3): a lightweight, read-only
    availability probe for the AGY delegation route (`agy` binary presence
    only -- no OAuth/keyring behavior change, per Out of Scope). AGY is an
    advisory/optional provider: absence degrades to the existing
    `agy_not_found` taxonomy value (`.claude/skills/gemini-cli-headless-
    delegation/references/failure-class-taxonomy.md`) with a non-AGY fallback
    route, rather than being reported `unavailable` outright -- the caller
    (`issue-refinement-loop`) decides escalate-vs-fallback per AC3, this
    preflight only reports observable availability."""
    if shutil.which("agy"):
        return {
            "status": ACTOR_CAPABILITY_READY,
            "reason_code": None,
            "fallback_route": None,
            "probe_execution_class": "agy_binary_which",
        }
    return {
        "status": ACTOR_CAPABILITY_DEGRADED,
        "reason_code": "agy_not_found",
        "fallback_route": "codebase_investigator_non_agy",
        "probe_execution_class": "agy_binary_which",
    }


def _spark_delegation_capability(spark_status: str) -> dict:
    """Actor-scoped entry (Issue #2340 AC2, retired by Issue #2651): maps
    `_spark_status()`'s verdict onto the shared ready/degraded/unavailable
    enum. `SPARK_NOT_REQUIRED` (ordinary callers, `spark_mode=None`) always
    reports ready -- no regression for callers that never requested Spark.
    Any non-None `spark_mode` (`SPARK_RETIRED`) always reports unavailable
    with a `spark_delegation_retired` reason -- there is no more
    fallback_only/degraded state, and no live binary/auth probe result can
    ever promote it back to ready."""
    if spark_status == SPARK_NOT_REQUIRED:
        return {
            "status": ACTOR_CAPABILITY_READY,
            "reason_code": None,
            "fallback_route": None,
            "probe_execution_class": "spark_directive_retired_probe",
        }
    return {
        "status": ACTOR_CAPABILITY_UNAVAILABLE,
        "reason_code": "spark_delegation_retired",
        "fallback_route": None,
        "probe_execution_class": "spark_directive_retired_probe",
    }


def _operation_route(operation: str) -> str:
    return "available" if operation in _KNOWN_OPERATION_ROUTES else "unavailable"


def _load_planned_operations(path: str | None) -> list[dict]:
    """Load and validate the caller-supplied planned-operations JSON.

    When `path` is omitted (None/empty), an empty list is the legitimate
    default (no planned mutation-requiring operations declared). When a path
    IS explicitly supplied, any malformation (unreadable file, invalid JSON,
    wrong top-level shape, malformed entry) is a caller input error and MUST
    raise `ValueError` rather than silently degrade to an empty list -- a
    silent `[]` would make a genuinely mutation-requiring operation set
    disappear and let `assess()` PASS as if no mutation were planned (P1-3
    fix)."""

    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot read planned-operations-json file {path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"planned-operations-json is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("planned-operations-json top-level value must be a JSON list")  # noqa: TRY004
    normalized = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError(f"planned-operations-json entry is not an object: {entry!r}")  # noqa: TRY004
        operation = entry.get("operation")
        phase = entry.get("phase")
        actor_role = entry.get("actor_role")
        if not isinstance(operation, str) or not operation:
            raise ValueError(
                f"planned-operations-json entry missing non-empty 'operation': {entry!r}"
            )
        if "requires_mutation" in entry and not isinstance(entry["requires_mutation"], bool):
            raise ValueError(
                f"planned-operations-json entry 'requires_mutation' must be a bool: {entry!r}"
            )
        normalized.append(
            {
                "phase": phase if isinstance(phase, str) else "unknown",
                "actor_role": actor_role if isinstance(actor_role, str) else "unknown",
                "operation": operation,
                # `requires_mutation` defaults to True: an operation
                # entry that doesn't explicitly mark itself read-only is
                # treated as mutation-requiring (fail closed).
                "requires_mutation": bool(entry.get("requires_mutation", True)),
            }
        )
    return normalized


def _append_probe_reason(reasons: list[str], probe_name: str, outcome: ProbeOutcome) -> None:
    if outcome.kind == PROBE_TIMEOUT:
        reasons.append(f"preflight_probe_timeout:{probe_name}")
    elif outcome.kind == PROBE_DEADLINE_EXHAUSTED:
        reasons.append(f"preflight_deadline_exhausted:{probe_name}")
    elif outcome.kind == PROBE_SPAWN_ERROR:
        reasons.append(f"preflight_probe_spawn_error:{probe_name}:{outcome.error_name}")
    elif outcome.kind == PROBE_NONZERO_EXIT:
        reasons.append(f"preflight_probe_nonzero_exit:{probe_name}:exit_{outcome.returncode}")
    elif outcome.kind == PROBE_MALFORMED_OUTPUT:
        reasons.append(f"preflight_probe_malformed_output:{probe_name}")


def assess(
    *,
    project_root: str,
    profile: str,
    repo: str,
    spark_mode: str | None,
    spark_fallback: str | None,
    planned_operations: list[dict],
    deadline_ns: int | None = None,
) -> dict:
    """Assess all capabilities using one absolute monotonic deadline.

    Direct callers retain compatibility: when no deadline is supplied this
    producer owns a local deadline with the same default budget as the root
    consumer's transport path.
    """
    deadline_ns = _local_deadline_ns() if deadline_ns is None else deadline_ns
    reasons: list[str] = []

    try:
        identity_ok = is_trusted_repo(repo)
    except RepositoryIdentityError:
        identity_ok = False
    if not identity_ok:
        # Never hand an unverified repo string to `gh`: block before any probe runs.
        return {
            "schema": SCHEMA,
            "profile": profile,
            "decision": DECISION_BLOCKED,
            "checks": {},
            "actor_capabilities": {},
            "reasons": ["repository_identity_mismatch"],
        }

    uv_result = trusted_uv_mod.check_trusted_uv(project_root)
    uv_ok = uv_result["status"] == trusted_uv_mod.STATUS_OK
    if not uv_ok:
        reasons.append(
            f"uv:{uv_result['status']}: install the pinned uv version to the account-home "
            "~/.local/bin (official standalone installer) or use the hostedtoolcache "
            "provisioned uv"
        )

    # Issue #2401 AC1 (Spark probe retired by Issue #2651): the required
    # GitHub probes (`github_auth`, then `github_repo_read` only when
    # `github_auth` completed) and the `controlled_github_read` probe (run
    # independently of the root `github_auth`/`github_repo_read` outcome)
    # never share their deadline budget with a Spark probe any more --
    # there is no live Spark probe in the decision path at all now.
    github_auth_outcome = _github_auth_probe(deadline_ns)
    _append_probe_reason(reasons, "github_auth", github_auth_outcome)
    github_auth = github_auth_outcome.kind == PROBE_COMPLETED
    github_repo_read = False
    if github_auth:
        github_repo_read_outcome = _github_repo_read_probe(repo, deadline_ns)
        _append_probe_reason(reasons, "github_repo_read", github_repo_read_outcome)
        github_repo_read = github_repo_read_outcome.kind == PROBE_COMPLETED
    if not github_auth:
        reasons.append(
            "github:auth_unavailable: run `gh auth login` (or ensure GH_TOKEN/GH_CONFIG_DIR "
            "are populated) before starting a workflow that reads or mutates GitHub state"
        )
    elif not github_repo_read:
        reasons.append(
            f"github:repo_read_unavailable: `gh repo view {repo}` failed; verify repository "
            "access for the authenticated account"
        )

    controlled_outcome = _controlled_github_read_probe(repo, deadline_ns)
    _append_probe_reason(reasons, "controlled_github_read", controlled_outcome)
    controlled_github_read = _controlled_github_read_from_outcome(controlled_outcome)
    controlled_github_unavailable = controlled_github_read["status"] == ACTOR_CAPABILITY_UNAVAILABLE
    if controlled_github_unavailable:
        reasons.append(
            "controlled_github_unavailable: the consumer-equivalent read-only GitHub probe "
            "(same sanitized env / trusted host the issue-editor / contract-update lane uses) "
            "failed; do not start those actors until this is resolved"
        )

    operations: dict[str, dict] = {}
    missing_mutation_route = False
    for entry in planned_operations:
        route = _operation_route(entry["operation"])
        operations[entry["operation"]] = {"route": route, "permission": "unverified"}
        requires_mutation = (
            entry.get("requires_mutation", True) or entry["operation"] in _MUTATION_OPERATION_NAMES
        )
        if route == "unavailable" and requires_mutation:
            missing_mutation_route = True
            reasons.append(
                f"github:operation_route_unavailable:{entry['operation']}: no implemented "
                f"native GitHub route for phase={entry.get('phase', 'unknown')} actor_role="
                f"{entry.get('actor_role', 'unknown')}; do not start this mutation-requiring phase"
            )

    # Issue #2651: GPT-5.3-Codex-Spark delegation is retired. A caller-
    # declared `spark_mode` is judged deterministically from the directive
    # alone (`_spark_status()`) -- no live binary/auth probe is spawned for
    # it any more (the former `spark_env_only` probe / deadline-sharing
    # concern from Issue #2401 no longer applies to this decision).
    spark_status = _spark_status(spark_mode)
    if spark_status == SPARK_RETIRED:
        reasons.append(
            "spark:retired: GPT-5.3-Codex-Spark delegation has been retired from this "
            "repository (Issue #2651); this workflow no longer selects or falls back to "
            "it -- remove the spark_mode/spark_fallback directive and use an ordinary "
            "SubAgent instead"
        )

    actor_capabilities = {
        "root_github_read": _root_github_read_capability(github_auth, github_repo_read),
        "controlled_github_read": controlled_github_read,
        "delegated_research_agy": _delegated_research_agy_capability(),
        "spark_delegation": _spark_delegation_capability(spark_status),
    }

    if (
        missing_mutation_route
        or not github_auth
        or not github_repo_read
        or controlled_github_unavailable
        or not uv_ok
        or spark_status == SPARK_RETIRED
    ):
        decision = DECISION_BLOCKED
    else:
        decision = DECISION_READY

    return {
        "schema": SCHEMA,
        "profile": profile,
        "decision": decision,
        "checks": {
            "uv": {
                "status": uv_result["status"],
                "reason": uv_result["reason"],
                "diagnostic": uv_result.get("diagnostic"),
            },
            "spark": {"status": spark_status},
            "github": {
                "auth": github_auth,
                "repo_read": github_repo_read,
                "operations": operations,
            },
        },
        "actor_capabilities": actor_capabilities,
        "reasons": reasons,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=SUPPORTED_PROFILES)
    parser.add_argument("--project-root", default=str(_REPO_ROOT))
    parser.add_argument("--repo", default=None)
    parser.add_argument("--spark-mode", default=None, choices=["required", "preferred", None])
    parser.add_argument("--spark-fallback", default=None, choices=["forbidden", "allowed", None])
    parser.add_argument("--planned-operations-json", default=None)
    parser.add_argument("--deadline-monotonic-ns", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.repo is None:
        try:
            args.repo = load_trusted_repo_slug()
        except RepositoryIdentityError as exc:
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "error": "repository_identity_unavailable",
                        "detail": str(exc),
                    }
                ),
                file=sys.stderr,
            )
            return 2

    try:
        planned_operations = _load_planned_operations(args.planned_operations_json)
    except ValueError as exc:
        error_payload = {
            "schema": SCHEMA,
            "error": "invalid_planned_operations_input",
            "detail": str(exc),
        }
        print(json.dumps(error_payload, ensure_ascii=False), file=sys.stderr)
        return 2

    result = assess(
        project_root=args.project_root,
        profile=args.profile,
        repo=args.repo,
        spark_mode=args.spark_mode,
        spark_fallback=args.spark_fallback,
        planned_operations=planned_operations,
        deadline_ns=args.deadline_monotonic_ns,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
