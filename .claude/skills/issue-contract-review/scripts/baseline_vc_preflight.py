#!/usr/bin/env python3
"""
Baseline Verification Command Preflight

Issue body の `## Verification Commands` セクションから VC を AC 別に抽出して単体実行し、
root-cause 分類（expected_fail / unexpected_pass / blocked / human_judgment）と
category / decision / confidence を含む JSON を返す。
"""

import argparse
import hashlib
import os
import json
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Shared AC / preflight-scope parser contract (同一の VC grammar 定義を利用)
_REPO_ROOT = Path(__file__).resolve().parents[4]
_VC_SYNTAX_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
if str(_VC_SYNTAX_DIR) not in sys.path:
    sys.path.insert(0, str(_VC_SYNTAX_DIR))

# Issue #2783: shared no-path marker predicate (`scripts/agent-ops/`). This
# module is a small pure policy library, not a grammar library import — do
# not import anything else from it here.
_AGENT_OPS_DIR = _REPO_ROOT / "scripts" / "agent-ops"
_AGENT_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))
import repo_identity as _repo_identity  # noqa: E402
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

from vc_contract_syntax import (  # noqa: E402
    VALID_PRE_FLIGHT_SCOPE_VALUES,
    parse_ac_marker_line,
    parse_preflight_scope_marker_line,
    extract_baseline_expect_annotation,
    extract_vc_role_annotation,
    extract_vc_regex_intent_annotation,
    detect_compound_command,
    parse_verification_commands_section,
    VcParseResult,
)
import pnpm_gate_registry as pnpm_gate_registry  # noqa: E402
import vc_runtime_history as _vc_runtime_history  # noqa: E402
from allowed_paths_policy import is_no_path_marker  # noqa: E402

# Issue #2782: level-2 section extraction authority (shared with
# contract_readiness_check.py::_extract_section_by_canonical_name()). This
# module does NOT import contract_readiness_check.py (that would form a
# cycle, since contract_readiness_check.py already imports this module) --
# both files delegate to prose_boundary_policy.py instead.
_CREATE_ISSUE_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(_CREATE_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS_DIR))

from prose_boundary_policy import extract_level2_section as _extract_level2_section  # noqa: E402


# Issue #1333 AC1: per-command timeout の named constant。
# 実測 test_baseline_vc_preflight.py 実行時間（約58秒、real 0m59.376s）を
# 安全マージン込みで上回る値（>= 90）を維持する。--timeout-seconds の
# argparse default はこの定数を参照し、run_contract_review_once.py 側も
# この定数を import して drift を防ぐ（AC2/AC3 参照）。
#
# Issue #2165: この per-VC-command timeout は、Verification Command 1本の
# 実行時間上限を決める最内層の値であり、ここでの過小値は上位層（reviewer
# transport 等）の deadline 設計と無関係に単体で誤検知を起こす。
# `issue-refinement-loop` skill 自身の full pytest suite VC
# （`uv run --locked pytest .claude/skills/issue-refinement-loop/tests/ -q`）
# は実測 111.07 秒であり、旧値 90 は単一の正当な長時間 VC でも超過する。
# 150 は実測値に約35%の安全マージンを載せた値（呼び出し元の
# `contract_readiness_check.py` / `run_root_review_pipeline.py` 側の
# wrapper timeout もこの値に整合させて拡大している。詳細は
# `reviewer_transport.py` の `PER_ATTEMPT_DEADLINE_SECONDS` docstring 参照）。
DEFAULT_TIMEOUT_SECONDS = 150

# ---------------------------------------------------------------------------
# Command-level timeout budget (Issue #2233)
# ---------------------------------------------------------------------------
#
# `DEFAULT_TIMEOUT_SECONDS` above is the single ROOT-owned per-command
# fallback value; `contract_readiness_check.py` / `run_contract_review_once.py`
# / `run_root_review_pipeline.py` import it (directly or via
# `compute_canonical_vc_plan()`) rather than re-defining an independent
# `150` scalar (Issue #2233 AC2). This section adds the `command_timeout_budget/v1`
# data type and its deterministic `static_fallback` / `static_policy`
# producers (`compute_command_timeout_budget()`), consumed by
# `compute_canonical_vc_plan()` below to populate the canonical plan's
# `command_budgets[]` field. This plan is now the SINGLE producer artifact:
# `baseline_vc_preflight.py`'s own executor (`_main_impl()` below),
# `contract_readiness_check.py`, `run_contract_review_once.py`, and
# `run_root_review_pipeline.py` all call `compute_canonical_vc_plan()` with
# the SAME pinned Issue body and verify each other's `plan_digest` (a
# canonical-serialization hash over the full versioned plan envelope)
# instead of independently re-deriving per-command scalars.
#
# Scope note (Issue #2233 Out of Scope, superseded by Issue #2254): full
# history-based (P50/P95) calibration was intentionally NOT implemented
# when this section was introduced (OWNER fix_delta P2 rejected a
# fabricated `sample_count=1` / `observed_p95_ms` derived from a bare
# `estimated_seconds` int, with no real evidence object -- worse than not
# having the source at all).
#
# Issue #2254 reintroduces `history_estimate` as a NEW, additive
# `command_timeout_budget/v1` source backed by GENUINE evidence: a local
# per-repository SQLite store of real subprocess durations
# (`vc_runtime_history.py`), a fixed `nearest_rank_v1` percentile contract
# (>= 5 eligible success samples, 30-day TTL, newest 50 samples), a
# right-censored timeout backoff floor, and a strictly RAISE-ONLY
# precedence (`history_estimate` can only ever WIN over `static_policy` /
# `static_fallback`, never lower the resolved timeout below them). See
# `compute_command_timeout_budget()`'s `history_snapshot` parameter below
# and `.claude/skills/issue-contract-review/references/vc-preflight.md`.

# Alias making the per-command role of `DEFAULT_TIMEOUT_SECONDS` explicit at
# call sites that reason about command-level budgets specifically.
DEFAULT_PER_COMMAND_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS

# Floor for any resolved per-command timeout (explicit override or
# fallback/policy source): guards against a degenerate 0/negative value
# reaching `run_command()` / `run_subprocess_with_cooperative_supervisor()`.
MIN_PER_COMMAND_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# Trusted static per-command timeout policy (Issue #2233 fix_delta P0-2)
# ---------------------------------------------------------------------------
#
# A deliberately hand-curated, repo-owned, version-controlled allowlist of
# EXACT VC command text known to legitimately exceed
# `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`. This is NOT a history-based (P50/P95)
# estimator (Out of Scope for this Issue) -- it is a trusted static policy
# authority: a human (via this source file's own review process) has pinned
# a specific, conservative ceiling for a specific, named command, backed by
# a real measurement recorded in the comment next to each entry. This is the
# mechanism the Issue #2233 Outcome allows ("static_fallback source の
# deterministic fallback" + a caller-supplied estimator hook) for producing
# a legitimately-larger-than-150s per-command budget WITHOUT implementing
# full telemetry calibration.
#
# Issue #2233 Background: `uv run --locked pytest
# .claude/skills/issue-refinement-loop/tests -v` was measured at
# `1556 passed in 271.31s` (real 4m32s) against Issue #2195's contract. This
# is not a one-off fluke -- it is a general VC pattern (the full
# `issue-refinement-loop` test suite) used by many Issues that touch that
# skill, and the suite only grows over time. 420s (>=1.5x the 271.31s
# measurement) is chosen as a conservative static ceiling; it is NOT derived
# from telemetry, and the ceiling itself must be manually revisited (raised,
# or a real history-based estimator substituted) once the suite grows past
# it again.
STATIC_TIMEOUT_POLICY_SCHEMA = "static_timeout_policy/v1"
STATIC_PER_COMMAND_TIMEOUT_POLICY: Dict[str, int] = {
    # Measured 271.31s (Issue #2233 Background, control-plane measurement
    # against Issue #2195's contract); 420s keeps a >=1.5x margin.
    "uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v": 420,
}

# Hard ceiling (Issue #2233 OWNER point 3 / Background; RAISED per fix_delta
# P0-2). A resolved per-command timeout that would exceed this value is
# REJECTED before any subprocess is launched
# (`CommandTimeoutExceedsPolicyError`, `command_timeout_exceeds_policy`),
# rather than silently honored -- this applies uniformly regardless of
# `source`. Chosen deliberately ABOVE the largest currently-curated
# `STATIC_PER_COMMAND_TIMEOUT_POLICY` value (with headroom for future
# entries) so a trusted-static-policy-backed command is never itself
# rejected by this ceiling, while still bounding the worst case a single
# command budget can ever claim.
MAX_PER_COMMAND_TIMEOUT_SECONDS = 600

assert MAX_PER_COMMAND_TIMEOUT_SECONDS >= max(
    STATIC_PER_COMMAND_TIMEOUT_POLICY.values(), default=0
), "MAX_PER_COMMAND_TIMEOUT_SECONDS must dominate every static policy entry"

# Fixed post-SIGTERM cleanup tail budgeted per command slot. This is the SAME
# value `contract_readiness_check.py`'s `_PER_VC_SLOT_CLEANUP_TAIL_SECONDS`
# used to hardcode independently (Issue #2233 AC2): that module now imports
# this constant instead.
CLEANUP_TAIL_SECONDS = 15

# Aggregate hard ceiling (Issue #2233 fix_delta P1-1: this is now WIRED into
# production enforcement inside `compute_canonical_vc_plan()` below, not
# merely informational). Independent of, and does NOT replace, the
# occurrence-count-based `MAX_VC_EXECUTION_SLOTS` policy_cap check
# (`VerificationBudgetExceedsPolicyError` in contract_readiness_check.py,
# Out of Scope: the #2207 aggregate formula itself is unchanged). This is a
# SEPARATE hard cap on the plan's own `aggregate_timeout_seconds` (the sum
# of each occurrence's ACTUAL resolved `timeout_seconds +
# cleanup_tail_seconds`), which can now legitimately exceed the old
# 150-second-per-command worst case once `static_policy` entries are used.
MAX_TOTAL_VERIFICATION_BUDGET_SECONDS = 40 * (
    MAX_PER_COMMAND_TIMEOUT_SECONDS + CLEANUP_TAIL_SECONDS
)

COMMAND_TIMEOUT_BUDGET_SCHEMA = "command_timeout_budget/v1"
# Bumped from "v1" (fix_delta): `execution_key_hash` renamed to
# `command_identity_hash` (P1-2), `static_policy` source added,
# `history_estimate` removed, `policy_clamped` now actually set.
COMMAND_TIMEOUT_BUDGET_ESTIMATOR_VERSION = "v2"

# Issue #2233 fix_delta: canonical VC plan envelope schema/version, bumped
# from the unversioned v1 shape (P1-3: `estimator_evidence_digest` is now a
# full-envelope canonical-serialization hash, and `plan_digest` /
# `command_occurrences` / `policy_version` are new top-level fields).
CANONICAL_VC_PLAN_SCHEMA = "canonical_vc_plan/v2"
CANONICAL_VC_PLAN_POLICY_VERSION = "v1"


class CommandTimeoutExceedsPolicyError(Exception):
    """Typed, non-retryable rejection: a resolved per-command timeout exceeds
    `MAX_PER_COMMAND_TIMEOUT_SECONDS`. Raised BEFORE any subprocess for that
    command is launched (Issue #2233 AC5)."""

    error_code = "command_timeout_exceeds_policy"

    def __init__(self, command_hash: str, requested_seconds: int, max_seconds: int):
        self.command_hash = command_hash
        self.requested_seconds = requested_seconds
        self.max_seconds = max_seconds
        super().__init__(
            f"command_hash={command_hash} requested_seconds={requested_seconds} "
            f"exceeds max_seconds={max_seconds} ({self.error_code})"
        )


class CommandTimeoutNonPositiveError(Exception):
    """Typed, non-retryable rejection: a resolved per-command timeout is
    <= 0 seconds, REGARDLESS of `source` (Issue #2233 fix_delta P2: a
    `resolved_seconds <= 0` explicit override was previously silently
    accepted, since only the upper ceiling was checked)."""

    error_code = "command_timeout_non_positive"

    def __init__(self, command_hash: str, requested_seconds: int, source: str):
        self.command_hash = command_hash
        self.requested_seconds = requested_seconds
        self.source = source
        super().__init__(
            f"command_hash={command_hash} requested_seconds={requested_seconds} "
            f"source={source} must be > 0 ({self.error_code})"
        )


class AggregateTimeoutExceedsPolicyError(Exception):
    """Typed, non-retryable rejection: a canonical plan's
    `aggregate_timeout_seconds` (the sum, over every VC occurrence, of that
    occurrence's own `timeout_seconds + cleanup_tail_seconds`) exceeds
    `MAX_TOTAL_VERIFICATION_BUDGET_SECONDS`. Raised BEFORE
    `compute_canonical_vc_plan()` returns -- i.e. BEFORE any subprocess for
    ANY command in the plan is launched (Issue #2233 AC5 aggregate
    enforcement, fix_delta P1-1)."""

    error_code = "aggregate_timeout_exceeds_policy"

    def __init__(self, aggregate_timeout_seconds: int, max_seconds: int):
        self.aggregate_timeout_seconds = aggregate_timeout_seconds
        self.max_seconds = max_seconds
        super().__init__(
            f"aggregate_timeout_seconds={aggregate_timeout_seconds} "
            f"exceeds max_seconds={max_seconds} ({self.error_code})"
        )


class VcPlanDigestMismatchError(Exception):
    """Typed, non-retryable rejection: a canonical VC plan's `plan_digest`,
    recomputed from the body a consumer actually received, does not match
    an `expected_plan_digest` supplied by the caller. Raised BEFORE any
    subprocess is launched (Issue #2233 fix_delta P0-1/P1-3: cross-process
    plan-identity verification -- protects against the body drifting
    between when a parent process computed the plan and when a child
    process, invoked as a separate subprocess, re-reads the (supposedly
    same) body)."""

    error_code = "vc_plan_digest_mismatch"

    def __init__(self, expected_digest: str, actual_digest: str):
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            f"expected_plan_digest={expected_digest} actual_plan_digest={actual_digest} "
            f"({self.error_code})"
        )


def compute_command_timeout_budget(
    command: str,
    *,
    override_seconds: Optional[int] = None,
    default_seconds: int = DEFAULT_PER_COMMAND_TIMEOUT_SECONDS,
    cleanup_tail_seconds: int = CLEANUP_TAIL_SECONDS,
    min_seconds: int = MIN_PER_COMMAND_TIMEOUT_SECONDS,
    max_seconds: int = MAX_PER_COMMAND_TIMEOUT_SECONDS,
    static_policy: Dict[str, int] = STATIC_PER_COMMAND_TIMEOUT_POLICY,
    history_snapshot: Optional[Dict[str, Any]] = None,
    cwd: str = ".",
    repo_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute a `command_timeout_budget/v1` entry for a single VC command.

    Precedence (Issue #2233 AC4, extended by Issue #2254 AC4
    raise-only precedence): `override_seconds` (an explicit
    `--timeout-seconds` CLI value) is a HARD GLOBAL override and always
    wins over every other source. Otherwise:

        resolved = min(max_seconds, max(static_base, history_candidate, timeout_backoff_floor))

    where `static_base` is the `static_policy` entry (if the command text
    matches) or `default_seconds` (`static_fallback`), and
    `history_candidate` / `timeout_backoff_floor` come from `history_snapshot`
    (Issue #2254 AC3/AC5). `history_estimate` is named as the resolved
    `source` ONLY when a history-derived value STRICTLY exceeds
    `static_base` -- if history is absent, degraded, or <= `static_base`,
    `source` stays `static_policy` / `static_fallback` (Issue #2254 In
    Scope: "history が static_base 以下の場合は provenance の source を
    static_policy または static_fallback のまま維持し、history_estimate を
    name-only で名乗らない").

    `static_policy` (Issue #2233 fix_delta P0-2): a trusted, hand-curated,
    repo-owned mapping of EXACT command text to a legitimately-larger
    per-command timeout (`source: static_policy`). This is the authority
    that lets a genuinely slow-but-legitimate VC (e.g. the 271.31s
    `issue-refinement-loop` test suite, Issue #2233 Background) resolve to
    a budget above `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS` without relying on
    history alone.

    `history_snapshot` (Issue #2254 AC1): an IMMUTABLE `history_snapshot/v1`
    dict (see `vc_runtime_history.build_history_snapshot()`), produced ONCE
    by a root-owned caller and passed in unchanged -- this function NEVER
    touches SQLite itself. `None` (the default; also every pre-#2254 call
    site until wired) behaves EXACTLY as before #2254 (`static_policy` /
    `static_fallback` only). `cwd` is used ONLY to derive the same
    `command_group_key` the snapshot's producer used to key its `records`
    dict (Issue #2254 AC2 identity separation) -- it does not affect
    `static_policy` / `static_fallback` resolution.

    Raises `CommandTimeoutExceedsPolicyError` (Issue #2233 AC5) BEFORE
    returning if the resolved value would exceed `max_seconds` -- this
    applies to EVERY source (override, static_policy, static_fallback, OR
    history_estimate) so the #2207 aggregate formula's per-command
    worst-case assumption can never be silently violated. This is also how
    Issue #2254 AC4's "history is capped at MAX_PER_COMMAND_TIMEOUT_SECONDS"
    requirement is enforced -- the `min(max_seconds, ...)` clamp above
    already guarantees the resolved value never exceeds `max_seconds`
    before this check runs.

    Raises `CommandTimeoutNonPositiveError` (Issue #2233 fix_delta P2)
    BEFORE returning if the resolved value is <= 0, regardless of `source`.
    """
    command_hash = compute_command_hash(command)
    policy_clamped = False

    # Issue #2254 AC1/AC2: look up this command's history record (if any)
    # from the caller-supplied immutable snapshot. This function NEVER
    # queries the store itself -- `history_snapshot` is a plain dict.
    history_record: Optional[Dict[str, Any]] = None
    history_store_status = "snapshot_absent"
    command_group_key: Optional[str] = None
    if history_snapshot is not None:
        history_store_status = history_snapshot.get("store_status", "unknown")
        # Issue #2254 fix_delta P0 blocker 2 (OWNER REQUEST_CHANGES
        # source-repo PR 2382 review comment 5458281756):
        # `repo_root`, when the caller resolved one for THIS SAME `cwd`,
        # must be threaded into the LOOKUP key exactly the way the
        # snapshot's own producer (`produce_immutable_history_snapshot()`)
        # threaded it into the snapshot's `records` KEYS -- otherwise a
        # lookup against an absolute-cwd key would never match a
        # repo-relative one (or vice versa).
        command_group_key = _vc_runtime_history.compute_command_group_key(
            command, cwd, repo_root=repo_root
        )
        history_record = (history_snapshot.get("records") or {}).get(command_group_key)

    history_sample_count = 0
    history_observed_p95_ms: Optional[int] = None
    history_candidate_seconds: Optional[int] = None
    timeout_backoff_floor_seconds: Optional[int] = None
    if history_record is not None:
        history_sample_count = int(history_record.get("eligible_sample_count") or 0)
        history_observed_p95_ms = history_record.get("observed_p95_ms")
        history_candidate_seconds = history_record.get("history_candidate_seconds")
        timeout_backoff_floor_seconds = history_record.get("timeout_backoff_floor_seconds")

    if override_seconds is not None:
        source = "explicit_override"
        resolved_seconds = int(override_seconds)
        sample_count = 0
        observed_p95_ms: Optional[int] = None
        history_backoff_applied = False
    else:
        static_source = "static_policy" if command.strip() in static_policy else "static_fallback"
        static_base = int(
            static_policy[command.strip()] if command.strip() in static_policy else default_seconds
        )
        history_candidates = [
            v for v in (history_candidate_seconds, timeout_backoff_floor_seconds) if v is not None
        ]
        history_best = max(history_candidates) if history_candidates else None

        # Issue #2254 AC4 raise-only precedence: history NEVER wins a tie
        # or a loss against `static_base` -- it must STRICTLY exceed it.
        if history_best is not None and history_best > static_base:
            source = "history_estimate"
            resolved_seconds = min(max_seconds, history_best)
            history_backoff_applied = (
                timeout_backoff_floor_seconds is not None
                and timeout_backoff_floor_seconds == history_best
            )
        else:
            source = static_source
            resolved_seconds = static_base
            history_backoff_applied = False
        sample_count = history_sample_count if source == "history_estimate" else 0
        observed_p95_ms = history_observed_p95_ms if source == "history_estimate" else None

    if resolved_seconds <= 0:
        raise CommandTimeoutNonPositiveError(command_hash, resolved_seconds, source)

    if resolved_seconds > max_seconds:
        raise CommandTimeoutExceedsPolicyError(command_hash, resolved_seconds, max_seconds)
    # Issue #2233: `min_seconds` is a documented floor for the
    # `static_fallback` / `static_policy` / `history_estimate` sources
    # ONLY. An `explicit_override` (Issue #2233 AC4 hard global override,
    # e.g. a fault-injection test's deliberately narrow `--timeout-seconds`)
    # is honored byte-for-byte and never silently raised to `min_seconds`
    # -- doing so would defeat the caller's explicit intent.
    if source != "explicit_override" and resolved_seconds < min_seconds:
        resolved_seconds = min_seconds
        policy_clamped = True

    estimator_input_digest = hashlib.sha256(
        json.dumps(
            {
                "command_hash": command_hash,
                "source": source,
                "timeout_seconds": resolved_seconds,
                "cleanup_tail_seconds": cleanup_tail_seconds,
                "estimator_version": COMMAND_TIMEOUT_BUDGET_ESTIMATOR_VERSION,
                "policy_clamped": policy_clamped,
                # Issue #2254: bind history evidence into the digest so two
                # budgets that resolve to the SAME timeout_seconds via
                # DIFFERENT evidence are still distinguishable.
                "sample_count": sample_count,
                "observed_p95_ms": observed_p95_ms,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    return {
        "schema": COMMAND_TIMEOUT_BUDGET_SCHEMA,
        "command_hash": command_hash,
        # Issue #2233 fix_delta P1-2: renamed from `execution_key_hash`.
        # This is deliberately NOT `compute_execution_key_hash()` (which
        # additionally incorporates normalized argv / cwd / runner env delta
        # / timeout / state epoch, to avoid conflating distinct executions
        # of textually-identical commands -- see that function's own
        # docstring below). `command_identity_hash` is scoped to command
        # TEXT identity only, which is all a plan-time (pre-execution)
        # budget can know.
        "command_identity_hash": command_hash,
        "timeout_seconds": resolved_seconds,
        "cleanup_tail_seconds": cleanup_tail_seconds,
        "source": source,
        "estimator_version": COMMAND_TIMEOUT_BUDGET_ESTIMATOR_VERSION,
        "estimator_input_digest": f"sha256:{estimator_input_digest}",
        "sample_count": sample_count,
        "observed_p95_ms": observed_p95_ms,
        "policy_clamped": policy_clamped,
        # Issue #2254 additive fields (never read by pre-#2254 consumers;
        # additive-only per Issue #2254 Out of Scope schema constraint).
        "command_group_key": command_group_key,
        "history_store_status": history_store_status,
        "history_backoff_applied": history_backoff_applied,
        "timeout_backoff_floor_seconds": timeout_backoff_floor_seconds,
    }


def collect_current_head_evidence(cwd: str, reviewed_head_sha: str) -> Dict[str, Any]:
    """Observe and certify one clean worktree against a reviewed commit object.

    The caller must provide the full immutable object ID. Git resolution is
    still used to prove that the object exists and is a commit, but aliases,
    symbolic refs, and abbreviated IDs are never canonicalized into acceptance.
    """
    base = {
        "head_sha": None,
        "reviewed_head_sha": reviewed_head_sha or None,
        "clean_before": False,
        "clean_after": None,
        "head_after_sha": None,
        "certified": False,
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": True,
        "errors": [],
    }
    if not reviewed_head_sha or isinstance(reviewed_head_sha, bool):
        base["errors"].append("reviewed_head_sha_missing_or_invalid")
        return base

    def git_text(*arguments: str) -> Tuple[Optional[str], Optional[str]]:
        result = subprocess.run(
            ["git", "-C", cwd, *arguments], capture_output=True, text=True
        )
        if result.returncode != 0:
            return None, result.stderr.strip() or "git_command_failed"
        return result.stdout.strip(), None

    observed, error = git_text("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}")
    if error:
        base["errors"].append(f"head_observation_failed:{error}")
        return base
    canonical_reviewed, error = git_text(
        "rev-parse", "--verify", "--end-of-options", f"{reviewed_head_sha}^{{commit}}"
    )
    if error:
        base["head_sha"] = observed
        base["errors"].append(f"reviewed_head_not_commit:{error}")
        return base
    if (
        reviewed_head_sha != canonical_reviewed
        or re.fullmatch(r"[0-9a-f]+", reviewed_head_sha) is None
    ):
        base["head_sha"] = observed
        base["errors"].append("reviewed_head_sha_not_full_oid")
        return base
    status, error = git_text("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if error:
        base["head_sha"] = observed
        base["reviewed_head_sha"] = canonical_reviewed
        base["errors"].append(f"cleanliness_observation_failed:{error}")
        return base

    base["head_sha"] = observed
    base["reviewed_head_sha"] = canonical_reviewed
    base["clean_before"] = status == ""
    if observed != canonical_reviewed:
        base["errors"].append("reviewed_head_sha_mismatch")
    if status != "":
        base["errors"].append("worktree_not_clean_before_execution")
    if not base["errors"]:
        base["certified"] = True
        base["stop_condition_triggered"] = False
    return base


def finalize_current_head_evidence(cwd: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Verify that a certified worktree did not drift during VC execution."""
    if not evidence.get("certified"):
        return evidence
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        text=True,
    )
    head_after = result.stdout.strip() if result.returncode == 0 else None
    clean_after = status.returncode == 0 and status.stdout == ""
    evidence["head_after_sha"] = head_after
    evidence["clean_after"] = clean_after
    if head_after != evidence.get("head_sha"):
        evidence["errors"].append("head_drift_during_execution")
    if not clean_after:
        evidence["errors"].append("worktree_not_clean_after_execution")
    evidence["certified"] = not evidence["errors"]
    evidence["stop_condition_triggered"] = not evidence["certified"]
    return evidence


def finalize_current_head_output(result: Dict[str, Any], evidence: Dict[str, Any]) -> None:
    """Make final status and safety flags consistent on every current-head exit path."""
    status = result.get("status")
    if status == "human_judgment":
        evidence["human_review_required"] = True
        evidence["stop_condition_triggered"] = True
        evidence["certified"] = False
    elif status == "blocked":
        evidence["stop_condition_triggered"] = True
        evidence["certified"] = False
    elif status == "pass" and not evidence.get("certified"):
        result["status"] = "blocked"
        evidence["stop_condition_triggered"] = True
    elif status == "pass":
        evidence["human_review_required"] = False
        evidence["stop_condition_triggered"] = False


def exit_code_for_status(status: str) -> int:
    """Map the final emitted status to a fail-closed process exit code."""
    return {"pass": 0, "blocked": 1, "human_judgment": 3}.get(status, 2)


def safety_fields(evidence_mode: str, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return stable bool safety fields for every JSON envelope."""
    if evidence_mode != "current-head":
        return {
            "evidence_mode": "baseline",
            "head_sha": None,
            "reviewed_head_sha": None,
            "fallback_detected": False,
            "human_review_required": False,
            "stop_condition_triggered": False,
        }
    evidence = evidence or {}
    return {
        "evidence_mode": "current-head",
        "head_sha": evidence.get("head_sha"),
        "reviewed_head_sha": evidence.get("reviewed_head_sha"),
        "head_after_sha": evidence.get("head_after_sha"),
        "clean_before": bool(evidence.get("clean_before")),
        "clean_after": evidence.get("clean_after"),
        "fallback_detected": bool(evidence.get("fallback_detected", False)),
        "human_review_required": bool(evidence.get("human_review_required", False)),
        "stop_condition_triggered": bool(evidence.get("stop_condition_triggered", True)),
    }


def emit_json(result: Dict[str, Any], evidence_mode: str, evidence: Optional[Dict[str, Any]] = None) -> None:
    """Emit the legacy envelope, extended only for current-head evidence."""
    if evidence_mode == "current-head":
        finalized_evidence = evidence or {}
        finalize_current_head_output(result, finalized_evidence)
        result.update(safety_fields(evidence_mode, finalized_evidence))
    print(json.dumps(result, indent=2))


def current_head_blocked_result(args: argparse.Namespace, evidence: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": args.issue or 0,
        "repo": args.repo,
        "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "source": {"kind": "current_head_validation", "body_sha256": ""},
        "status": "blocked",
        "summary": {
            "expected_fail": 0,
            "unexpected_pass": 0,
            "blocked": 1,
            "human_judgment": 0,
            "extraction_errors": 0,
        },
        "results": [],
        "errors": [message, *evidence.get("errors", [])],
        "diagnostic_report": not_computed_diagnostic_report(),
    }


def get_issue_body(issue_number: int, repo: str) -> Tuple[Optional[str], Optional[str]]:
    """
    GitHub API から Issue body を取得

    戻り値: (body, error_code)
      error_code: None (成功), "gh_auth_failed", "gh_repo_not_found", "gh_other_error"
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "body",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("body"), None
        else:
            stderr_lower = result.stderr.lower()
            if "not authenticated" in stderr_lower or "authentication failed" in stderr_lower:
                return None, "gh_auth_failed"
            elif "not found" in stderr_lower or "could not resolve" in stderr_lower:
                return None, "gh_repo_not_found"
            else:
                return None, "gh_other_error"
    except json.JSONDecodeError:
        return None, "gh_json_parse_error"
    except subprocess.TimeoutExpired:
        return None, "gh_timeout"
    except Exception:
        return None, "gh_other_error"


def read_body_file(path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    ファイルから Issue body を読み込み

    戻り値: (body, error_code)
      error_code: None (成功), "body_file_not_found", "body_parse_error"
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), None
    except FileNotFoundError:
        return None, "body_file_not_found"
    except Exception:
        return None, "body_parse_error"


def extract_verification_commands_section(body: str) -> Optional[str]:
    """
    body から `## Verification Commands` セクションを抽出する。

    section boundary algorithm 自体は持たず、単一 authority である
    `prose_boundary_policy.extract_level2_section()`（#2782）へ委譲する
    （GFM closing-hash 見出し・section 内 nested 見出し・fenced code block
    内の擬似 `##` 行のいずれに対しても誤抽出しない）。

    Return contract（#2782 AC4）:
      None            -- heading 不在（`## Verification Commands` が body に
                          存在しない）
      ""              -- heading 存在するが本文が空、または空白のみ
      non-empty str   -- heading 存在し、本文に実行可能な content がある

    presence のみを判定する consumer は `is not None` を使うこと。
    truthiness / `bool()` は "" と None を区別できないため使用しない。
    """
    return _extract_level2_section(body, "Verification Commands")


_ALLOWED_PATH_BULLET_RE = re.compile(r"^\s*[-+*]\s+")
_ALLOWED_PATH_CODE_RE = re.compile(r"^`([^`]+)`\s*(?:[（(].*)?$")


def _normalize_allowed_path_entry(line: str) -> str:
    """Normalize one Allowed Paths list entry to a bare path string.

    Handles:
    - bullet markers: -, +, * (GFM spec)
    - backtick-wrapped: `path`（注釈）or `path` (description)
    - trailing full-width/half-width parens annotation
    """
    s = line.strip()
    # Strip bullet marker (-, +, *)
    s = _ALLOWED_PATH_BULLET_RE.sub("", s)
    s = s.strip()
    # Strip backtick wrapping with optional annotation
    m = _ALLOWED_PATH_CODE_RE.match(s)
    if m:
        return m.group(1).strip()
    # Strip trailing full-width or half-width paren annotation (no backtick)
    s = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", s)
    return s.strip()


def extract_allowed_paths(body: str) -> List[str]:
    """
    Parse the `## Allowed Paths` section from an Issue body.

    Returns a list of path strings (stripped, without leading '- ').
    Empty list if section not found or no paths listed.

    Example section:
      ## Allowed Paths
      - .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py
      - .claude/skills/issue-contract-review/tests/
    """
    match = re.search(
        r"^##\s+Allowed Paths\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []

    section = match.group(1)

    # Issue #2783 AC8: a code-fence-wrapped no-path marker (e.g.
    # ```\n(none)\n```) spans multiple lines, so it must be recognized as a
    # single wrapped entry BEFORE the per-line loop below (which otherwise
    # would see the fence delimiters and inner content as separate lines).
    # Judged identically to a bullet-wrapped marker: is_no_path_marker()
    # itself performs the fence-stripping; here we only decide whether to
    # blank the whole fence block out of the section (0 paths) or leave it
    # untouched for the per-line loop's existing (unrelated) handling.
    def _blank_fence_if_marker(fence_match: "re.Match[str]") -> str:
        if is_no_path_marker(fence_match.group(0)):
            return "\n" * fence_match.group(0).count("\n")
        return fence_match.group(0)

    section = re.sub(r"```[^\n]*\n.*?```", _blank_fence_if_marker, section, flags=re.DOTALL)

    paths = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Lines starting with a bullet marker (-, +, *) are list items
        if _ALLOWED_PATH_BULLET_RE.match(stripped):
            # Issue #2783: judge no-path marker BEFORE annotation
            # normalization (fixed order: wrapper strip -> marker match ->
            # ordinary annotation normalization). A marker line contributes
            # 0 paths.
            if is_no_path_marker(stripped):
                continue
            path = _normalize_allowed_path_entry(stripped)
            if path:
                paths.append(path)
        # Also handle lines without bullet prefix (plain paths or backtick-wrapped)
        elif not stripped.startswith("#"):
            if is_no_path_marker(stripped):
                continue
            path = _normalize_allowed_path_entry(stripped)
            if path:
                paths.append(path)

    return paths


def extract_fenced_bash_blocks(section: str) -> List[str]:
    """
    セクションから ```bash ... ``` ブロックを抽出。

    B4: canonical format は ```bash のみ。unlabeled fence (```) は無視する。
    """
    blocks = []
    for match in re.finditer(r"```bash[ \t]*\n(.*?)```", section, re.DOTALL):
        blocks.append(match.group(1).rstrip("\n"))
    return blocks


def find_unlabeled_fenced_blocks(section: str) -> List[str]:
    """
    B4: unlabeled fence (``` without language specifier) を検出。

    戻り値: unlabeled fence の内容リスト（警告用）
    """
    unlabeled = []
    for match in re.finditer(r"```[ \t]*\n(.*?)```", section, re.DOTALL):
        unlabeled.append(match.group(1).rstrip("\n"))
    return unlabeled


def extract_preflight_scope_marker(lines: List[str], target_line_idx: int) -> Optional[str]:
    """
    VC コマンド行（target_line_idx）の直前行から `# preflight-scope: <value>` marker を抽出。

    戻り値: marker value ('pr_review_only' / 'runtime_only' / <invalid-value>) または None

    NB2: If the value does not match the allowed set {pr_review_only, runtime_only},
    it is returned as-is for downstream classification to handle as human_judgment.
    """
    if target_line_idx <= 0:
        return None
    prev_line = lines[target_line_idx - 1].strip()
    marker, _ = parse_preflight_scope_marker_line(prev_line)
    return marker


def parse_commands_from_block(
    block: str,
) -> List[
    Tuple[
        Optional[str], str, int, Optional[str],
        Optional[str], Optional[str], Optional[str],
        Optional[int], Optional[str],
    ]
]:
    """
    bash ブロックからコマンドを抽出。
    AC マーカーとコマンドの行番号と preflight-scope marker と vc-regex-intent annotation を返す。

    戻り値: [(ac_label, command, line_number, preflight_scope, vc_regex_intent,
               baseline_expect, vc_role, annotation_line_no, annotation_raw), ...]
      - ac_label: "AC1", "AC2", ... または None
      - command: raw command ($ prefix 除去済み、suffix marker 除去済み)
      - line_number: block 内での行番号
      - preflight_scope: 'pr_review_only' / 'runtime_only' / None
      - vc_regex_intent: 'literal-pipe-ok' / None (AC3: Issue #589)
      - baseline_expect: 'pass' / 'fail' / 'deferred' / None (Issue #889)
      - vc_role: role string / None (Issue #889)
      - annotation_line_no: 1-based line number of baseline-expect annotation / None
      - annotation_raw: raw annotation line text / None
    """
    commands = []
    lines = block.split("\n")
    current_ac = None

    for i, line in enumerate(lines, start=1):
        # AC マーカーの抽出: `# AC<N>`（strict）
        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None:
            if is_valid:
                current_ac = ac_label
            # strict marker lines are annotation, not commands
            if line.strip().startswith("#"):
                continue

        # preflight-scope marker はスキップ（コマンドではない）
        preflight_scope_marker, _ = parse_preflight_scope_marker_line(line)
        if preflight_scope_marker is not None:
            continue

        # vc-regex-intent annotation はスキップ（コマンドではない）
        if re.match(r"^\s*#\s*vc-regex-intent:\s*\S+", line):
            continue

        if line.strip().startswith("#"):
            # その他のコメント行はコマンドではない
            continue

        # コマンド行の抽出（$ prefix 除去）
        cmd_match = re.match(r"^\s*\$\s+(.+)$", line)
        if not cmd_match:
            # $ がない行でも、$ でなく非コメント・非空行の場合は取得
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                cmd_match = re.match(r"^\s*(.+)$", line)

        if cmd_match:
            cmd = cmd_match.group(1).strip()
            if cmd and not cmd.startswith("#"):
                # B4: inline suffix `# AC<N>` を検出して ac_label を上書き、suffix を除去
                suffix_match = re.search(r"\s+#\s*(.+)\s*$", cmd)
                if suffix_match:
                    suffix_label, suffix_is_valid = parse_ac_marker_line(f"# {suffix_match.group(1)}")
                    if suffix_label is not None and suffix_is_valid:
                        current_ac = suffix_label
                        cmd = re.sub(r"\s+#\s*AC\d+\s*$", "", cmd).strip()

            # 直前行から preflight-scope marker を抽出
            preflight_scope = extract_preflight_scope_marker(lines, i - 1)

            # 直前行から vc-regex-intent annotation を抽出 (AC3: Issue #589)
            vc_regex_intent = extract_vc_regex_intent_annotation(lines, i - 1)

            # 直前の連続 annotation ブロックから baseline-expect / vc-role を抽出 (Issue #889)
            baseline_expect, annotation_line_no, annotation_raw = extract_baseline_expect_annotation(lines, i - 1)
            vc_role = extract_vc_role_annotation(lines, i - 1)

            commands.append((
                current_ac, cmd, i, preflight_scope,
                vc_regex_intent, baseline_expect, vc_role,
                annotation_line_no, annotation_raw,
            ))

    return commands


# Type alias documenting the legacy 9-tuple command shape shared by
# `parse_commands_from_block()` (kept only for `compute_duplicate_diagnostic_report()`,
# a diagnostic-only display path -- Issue #2788 Out of Scope) and the shared
# adapter below (Issue #2788 AC1/AC2/AC7/AC12).
CommandTuple = Tuple[
    Optional[str], str, int, Optional[str],
    Optional[str], Optional[str], Optional[str],
    Optional[int], Optional[str],
]


def _ac_refs_to_scalar_label(ac_refs: "set") -> Optional[str]:
    """Deterministically collapse a `VcCommandEntry.ac_refs` set into the
    scalar `ac_label` normal execution / `_build_result_item()`'s `"ac"`
    field expect (Issue #2788 AC11).

    - Empty set -> None (matches legacy `parse_commands_from_block()`'s
      `current_ac = None` when no marker preceded the command).
    - Single ref -> that ref unchanged (e.g. "AC1").
    - Multiple refs (grouped marker, e.g. `# AC2, AC3`) -> a comma-joined,
      NUMERICALLY sorted string (e.g. "AC2,AC3") -- deterministic regardless
      of the set's iteration order. This is purely a DISPLAY/labelling
      concatenation: it does not fan out into multiple commands or
      subprocess launches -- exactly ONE `VcCommandEntry` (hence exactly
      one entry in the tuple list this function's caller builds) already
      exists per source `$ command` line, regardless of how many AC labels
      reference it.
    """
    if not ac_refs:
        return None
    if len(ac_refs) == 1:
        return next(iter(ac_refs))

    def _ac_sort_key(label: str) -> int:
        try:
            return int(label[2:]) if label.startswith("AC") else 0
        except ValueError:
            return 0

    return ",".join(sorted(ac_refs, key=_ac_sort_key))


def _command_entries_from_shared_parser(
    vc_section: str,
) -> Tuple[List[CommandTuple], VcParseResult]:
    """Thin shared adapter (Issue #2788 AC1/AC2/AC7/AC12): the SINGLE
    conversion point from `vc_contract_syntax.parse_verification_commands_section()`'s
    canonical `VcParseResult.commands` (`list[VcCommandEntry]`) into the
    SAME legacy 9-tuple shape `parse_commands_from_block()` historically
    produced -- `(ac_label, command, line_no, preflight_scope,
    vc_regex_intent, baseline_expect, vc_role, annotation_line_no,
    annotation_raw)`.

    Normal execution (`_main_impl()`), `compute_canonical_vc_plan()`, and
    `_distinct_command_texts_from_body()` all call THIS function instead of
    each independently walking `extract_fenced_bash_blocks()` +
    `parse_commands_from_block()`'s legacy grammar -- so all three now
    consume the SAME canonical VC command grammar authority `--static-only`
    already used, and an explanatory non-`$` line inside a bash fence is
    NEVER promoted to a runnable subprocess candidate in any of the three
    paths (it appears only in the returned `VcParseResult.errors` /
    `.static_errors`, never in `VcParseResult.commands`).

    No new independent VC grammar parser is introduced by this function --
    it performs pure data reshaping over the shared parser's own output.

    Issue #2788 fix_delta P2: the tuple's `line_no` slot (index 2) is
    sourced from `entry.block_line_number` (block-relative -- i.e. relative
    to the first line after the command's own enclosing ```bash opening
    fence line), NOT `entry.line_number` (section-relative -- i.e. relative
    to the whole `## Verification Commands` section content). This
    preserves the EXACT pre-migration `results[].line` coordinate system
    `parse_commands_from_block()` produced, and keeps it consistent with
    `annotation_source_line` (already block-relative, since
    `extract_baseline_expect_annotation()` is invoked against
    `bash_block_lines` both before and after this migration) -- so a single
    result item's `line` and `annotation_source.line` always refer to the
    SAME coordinate system instead of silently drifting between
    section-relative and block-relative.

    Returns `(command_tuples, parse_result)` so a caller can ALSO inspect
    `parse_result.static_errors` (e.g. to reject a non-canonical body
    BEFORE building any subprocess candidate, Issue #2788 AC3) without
    re-parsing the section a second time.
    """
    parse_result = parse_verification_commands_section(vc_section)
    command_tuples: List[CommandTuple] = []
    for entry in parse_result.commands:
        command_tuples.append((
            _ac_refs_to_scalar_label(entry.ac_refs),
            entry.command,
            entry.block_line_number,
            entry.preflight_scope,
            entry.vc_regex_intent,
            entry.baseline_expect,
            entry.vc_role,
            entry.annotation_source_line,
            entry.annotation_source_raw,
        ))
    return command_tuples, parse_result


def compute_command_hash(command: str) -> str:
    """コマンドの SHA-256 hash を計算"""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def compute_execution_key_hash(
    argv: List[str],
    cwd: str,
    runner_env_delta: Dict[str, str],
    timeout_seconds: int,
    state_epoch: int = 0,
) -> str:
    """Issue #1338: dedup key covering normalized argv + cwd + runner_env_delta + timeout + state_epoch.

    PR #1508 review (P0-1): the execution key MUST also cover a `state_epoch`
    -- a monotonically increasing counter that advances every time a
    "barrier" command (any command that is not a validated pure observation;
    see _is_parallel_eligible_command) executes. Without this, two
    textually-identical, side-effect-free observations (e.g.
    `test -d fixture/__pycache__`) that bracket a stateful command (e.g.
    `python3 -m py_compile fixture/state_probe.py`) would previously hash
    identically and the second observation would be incorrectly replayed
    from the first's stale pre-barrier snapshot instead of being re-executed
    against the new on-disk state.

    Distinct from compute_command_hash() (raw command string hash; existing
    behavior/meaning unchanged). Two commands whose execution_key_hash matches
    are guaranteed to run under identical conditions (same argv / cwd / env /
    timeout / state_epoch), so the second invocation may safely replay the
    first's execution outcome (exit_code/stdout/stderr/duration_ms/
    runner_env_delta) instead of re-launching the subprocess. classify_result()
    /decision are NEVER copied from the source; callers must recompute them
    per-AC using each AC's own raw_command/annotations.
    """
    normalized_cwd = os.path.normpath(os.path.abspath(cwd))
    normalized_env = sorted(runner_env_delta.items())
    payload = json.dumps(
        {
            "argv": list(argv),
            "cwd": normalized_cwd,
            "env": normalized_env,
            "timeout_seconds": timeout_seconds,
            "state_epoch": state_epoch,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rg_operand_is_safe_target(cwd: str, operand: str, allowed_paths: List[str]) -> bool:
    """Issue #1338 P0-2 fix (PR #1508 review): a single `rg` PATH operand is
    only a safe (bounded, non-racy) parallel/dedup target if it is a
    repo-relative, existing, non-special filesystem entry that is either a
    regular file (any repo-relative location) or a directory contained
    within the Issue's `## Allowed Paths` (recursion is bounded to that
    Allowed Path, never to the whole repo). FIFOs, sockets, and device
    files are always rejected; `-` (stdin) is rejected by the caller before
    reaching here.
    """
    if operand == "-":
        return False
    normalized = _normalize_repo_relative_path_strict(operand)
    if normalized is None:
        return False
    try:
        resolved_cwd = Path(cwd).resolve(strict=True)
    except OSError:
        return False
    resolved_target = (resolved_cwd / normalized).resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_cwd)
    except ValueError:
        return False  # escapes the repo root
    try:
        st = os.stat(resolved_target)
    except OSError:
        return False  # must already exist (unverifiable targets are not "pure")
    mode = st.st_mode
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return False
    if stat.S_ISREG(mode):
        return True
    if stat.S_ISDIR(mode):
        return _rg_path_operands_all_within_allowed_paths([operand], allowed_paths or [])
    return False


def _validate_rg_parallel_command(command: str, cwd: str, allowed_paths: List[str]) -> bool:
    """Issue #1338 P0-2 fix (PR #1508 review): full validation gate for an
    `rg` command before it may be treated as a pure/dedup/parallel-eligible
    observation.

    Requires ALL of:
      - >= 1 explicit PATH operand (a bare `rg PATTERN` with no path is
        rejected: it would search the whole repo AND, with no path operand
        and no piped input, `rg` reads from stdin -- subprocess.Popen
        defaults to inheriting the parent's stdin, so concurrent `rg`
        invocations with no explicit path are a stdin race).
      - no `-` (explicit stdin) operand.
      - every operand is a repo-relative, existing, non-special target,
        validated by _rg_operand_is_safe_target() (rejects absolute paths,
        `..` traversal, repo-external targets, FIFOs/devices/sockets, and
        unbounded directory recursion outside `## Allowed Paths`).
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv or Path(argv[0]).name != "rg":
        return False
    path_operands = _rg_extract_path_operands(argv)
    if not path_operands:
        return False
    return all(_rg_operand_is_safe_target(cwd, op, allowed_paths) for op in path_operands)


def _is_parallel_eligible_command(
    command: str, cwd: str = ".", allowed_paths: Optional[List[str]] = None
) -> bool:
    """Issue #1338 AC5, narrowed by PR #1508 review (P0-1/P0-2): predicate
    for bounded-parallel/dedup-safe PURE read-only VC observations.

    Only two shapes are eligible:
      - exact 'test -f|-d|-s PATH' (3 argv tokens)
      - `rg` with a fully validated bounded path operand (see
        _validate_rg_parallel_command)

    `grep` / `egrep` / `fgrep` are INTENTIONALLY EXCLUDED (P0-2 fix): the
    prior basename-only classification allowed them into the parallel/dedup
    pool with no argument-count / path-operand / stdin validation at all,
    and subprocess.Popen defaults to inheriting the parent's stdin, so
    concurrent invocations without an explicit path operand could race on
    shared stdin. They remain executable (still listed in _ALLOWED_COMMANDS
    for serial preflight execution) but are never batched or dedup-replayed.

    Everything else (pnpm, uv run pytest, pytest, gh, git,
    github_metadata_assert, python3 -m py_compile, etc.) is treated as a
    state barrier by the caller: never eligible for dedup or concurrent
    execution (rate-limit / cache-contention / state-mutation concerns).
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    basename = Path(argv[0]).name
    if basename == "test" and len(argv) == 3 and argv[1] in ("-f", "-d", "-s"):
        return True
    if basename == "rg":
        return _validate_rg_parallel_command(command, cwd, allowed_paths or [])
    return False


# ---------------------------------------------------------------------------
# Canonical VC occurrence planner (Issue #2207)
# ---------------------------------------------------------------------------
#
# Side-effect-free planner shared by budget derivation
# (`contract_readiness_check.py`'s `derive_review_budget()`) and THIS
# module's own executor loop below. Given pinned Issue body bytes, computes
# a canonical plan binding a `body_sha256` to the conservative upper bound
# of VC subprocess launches the executor may actually spawn for that body.
#
# This function NEVER executes a subprocess and NEVER performs I/O beyond
# reading the in-memory `body` string it is given. It reuses the SAME
# normal-execution parsing primitives (`extract_verification_commands_section()`
# / `_command_entries_from_shared_parser()` / `_is_parallel_eligible_command()`)
# the executor loop in `main()` below uses, so the plan cannot silently
# drift from what the executor actually does (Issue #2207 OWNER finding:
# "budget/execution parser drift"; Issue #2788 additionally unified BOTH
# onto the SAME `vc_contract_syntax.parse_verification_commands_section()`
# canonical grammar authority `--static-only` already used, instead of the
# legacy `extract_fenced_bash_blocks()` + `parse_commands_from_block()`
# grammar, closing a SEPARATE authority-drift gap where an explanatory
# non-`$` line could be promoted to a command/budget candidate here or in
# normal execution while `--static-only` already rejected it).
#
# Counting semantics (OWNER-reviewed redesign, Issue #2207):
#
# - `command_occurrence_count`: the RAW number of VC command lines extracted
#   from the `## Verification Commands` section, with NO dedup applied at
#   all (pure or non-pure). This is a diagnostic total occurrence count --
#   every duplicate non-pure command occurrence counts (AC2), and distinct
#   pure commands (`rg` / exact `test -f|-d|-s`) are included too (AC3).
#
# - `launch_upper_bound`: the conservative upper bound of ACTUAL subprocess
#   launches the executor may perform for this body. Non-pure commands are
#   NEVER dedup-replayed by the executor, so every occurrence counts as a
#   separate launch and also advances the executor's `state_epoch` (state
#   barrier semantics). Pure commands ARE dedup-replayed by the executor
#   when an identical (command-text) observation recurs within the SAME
#   state epoch (no non-pure barrier between them) -- so a repeated
#   observation within one epoch is counted once, but the SAME pure command
#   observed again after a barrier belongs to a new epoch and is counted as
#   a separate launch (AC4). This mirrors this module's own `_state_epoch`
#   / `_dedup_seen_keys` bookkeeping in `main()` below, without executing
#   anything.
#
# `N = max(2, command_occurrence_count)` is the value
# `contract_readiness_check.derive_review_budget()` consumes to derive the
# timeout envelope (Issue #2207 OWNER P1-3, PR #2221 REQUEST_CHANGES: the
# Issue #2207 Outcome/AC5 contract fixes the denominator as
# `command_occurrence_count`, the raw VC occurrence count -- NOT
# `launch_upper_bound`, the dedup-aware actual-launch upper bound also
# reported on this same plan for other, non-budget uses).

# Issue #2207 AC7 / OWNER P1-3: fixed policy ceiling applied to
# `command_occurrence_count` (not `launch_upper_bound`). Chosen so a
# reasonable worst-case aggregate timeout stays bounded even at the ceiling
# (40 * (150 + 15) + 20 = 6620s ~ 110 minutes) while comfortably exceeding
# any currently-observed live Issue body VC count. A body whose canonical
# plan exceeds this MUST be rejected before any subprocess is launched
# (`verification_budget_exceeds_policy`, non-retryable).
MAX_VC_EXECUTION_SLOTS = 40

VC_PLAN_PARSER_CONTRACT_VERSION = "v1"

# Matches this module's `--max-workers` default (fully serial execution).
# The canonical plan does not itself change execution concurrency; it
# reports the value the executor would use unless the caller overrides it.
_VC_PLAN_DEFAULT_MAX_WORKERS = 1


def _canonicalize_for_digest(payload: Dict[str, Any]) -> bytes:
    """Deterministic, key-sorted, compact JSON serialization (JCS-style;
    Issue #2233 fix_delta P1-3) used to bind a full versioned envelope to a
    single hash. `sort_keys=True` + no whitespace ensures the SAME logical
    payload always serializes to the SAME bytes, so two independent
    processes computing this over identical input data always agree."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_canonical_vc_plan(
    body: str,
    *,
    policy_cap: int = MAX_VC_EXECUTION_SLOTS,
    max_workers: int = _VC_PLAN_DEFAULT_MAX_WORKERS,
    per_command_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    global_override_seconds: Optional[int] = None,
    cwd: str = ".",
    allowed_paths: Optional[List[str]] = None,
    history_snapshot: Optional[Dict[str, Any]] = None,
    repo_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute the canonical, side-effect-free VC plan for `body`.

    Returns a dict with `schema` / `policy_version` / `body_sha256` /
    `parser_contract_version` / `command_occurrence_count` /
    `launch_upper_bound` / `per_command_timeout_seconds` / `max_workers` /
    `policy_cap` / `command_occurrences` / `command_budgets` /
    `aggregate_timeout_seconds` / `estimator_evidence_digest` /
    `plan_digest` (Issue #2233 AC1/AC2/AC3/AC6, fix_delta P0-1/P1-3).

    This function is the SINGLE root-owned producer of the canonical VC
    plan. `contract_readiness_check.py`, `run_root_review_pipeline.py`,
    `run_contract_review_once.py`, and `baseline_vc_preflight.py`'s OWN
    executor (`_main_impl()` below) all call this SAME function (directly,
    or -- across a subprocess boundary -- by recomputing it from the exact
    same pinned body bytes and verifying `plan_digest` against an
    `expected_plan_digest` the parent process passed through
    `--expected-plan-digest`) instead of each independently re-deriving
    per-command timeout scalars (Issue #2233 fix_delta P0-1).

    `command_occurrences` is the ORDERED (not deduplicated) list of every VC
    command line extracted from the `## Verification Commands` section, as
    `{"command_hash": ..., "is_pure": ...}`. `command_budgets` is a list of
    `command_timeout_budget/v1` entries, one per DISTINCT command text (see
    `compute_command_timeout_budget()`), in first-occurrence order.
    `global_override_seconds` (Issue #2233 AC4), when given, is a hard
    override applied to every command's budget (`source: explicit_override`);
    this mirrors `baseline_vc_preflight.py`'s own `--timeout-seconds` CLI
    precedence so a caller of THIS function observes the identical override
    semantics the executor applies.

    `aggregate_timeout_seconds` sums, over every VC OCCURRENCE (not
    deduplicated), `timeout_seconds + cleanup_tail_seconds` from that
    occurrence's command budget -- this now reflects the REAL resolved
    per-command budgets (including any `static_policy` entries, which may
    legitimately exceed `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`), not merely
    the worst-case-bounded value the (unchanged) #2207
    `derive_review_budget()` formula assumes.

    `plan_digest` (== `estimator_evidence_digest`, fix_delta P1-3) is a
    canonical-serialization (JCS-style, key-sorted, whitespace-free JSON)
    hash over the FULL versioned plan envelope -- `schema` /
    `policy_version` / `body_sha256` / `parser_contract_version` /
    `command_occurrences` / `command_budgets` / `aggregate_timeout_seconds` /
    `max_workers` / override provenance -- so ANY change to any of those
    fields changes the digest (unlike the prior v1 implementation, which
    only hashed a sorted list of small per-distinct-command digests and
    therefore missed changes to occurrence count/order, cleanup tail,
    aggregate, max_workers, body SHA, etc).

    `repo_root` (Issue #2254 fix_delta P0 blocker 2/3): the SAME
    worktree-root string (see `resolve_repo_root_for_history()` below) the
    caller resolved for THIS SAME `cwd` and passed to
    `produce_immutable_history_snapshot()` when building `history_snapshot`
    -- threaded through to every `compute_command_timeout_budget()` call so
    the LOOKUP key matches the snapshot's own `records` KEYS. `None`
    preserves pre-fix_delta (absolute-cwd) behavior.

    `history_snapshot` (Issue #2254 AC1): an IMMUTABLE `history_snapshot/v1`
    dict a root-owned caller produced ONCE (see
    `produce_immutable_history_snapshot()` below /
    `vc_runtime_history.build_history_snapshot()`) and threads through to
    every `compute_command_timeout_budget()` call this function makes.
    Because this function itself NEVER queries the history store (it only
    reads the given dict), two calls with the SAME `body` + SAME
    `history_snapshot` object (or an equal dict, e.g. round-tripped through
    `vc_runtime_history.write_history_snapshot_file()` /
    `load_history_snapshot_file()` across a subprocess boundary) always
    produce the SAME `plan_digest` -- even if the underlying SQLite store
    has since been written to or a TTL boundary has since passed (Issue
    #2254 AC1 snapshot determinism). `None` (the default) preserves
    pre-#2254 behavior exactly (`static_policy` / `static_fallback` only,
    no `history_estimate` source ever resolved).

    Raises `CommandTimeoutExceedsPolicyError` / `CommandTimeoutNonPositiveError`
    (Issue #2233 AC5 / fix_delta P2) BEFORE returning if any resolved
    command budget would exceed the per-command hard maximum or resolve to
    <= 0 seconds. Raises `AggregateTimeoutExceedsPolicyError` (fix_delta
    P1-1) BEFORE returning if the plan's `aggregate_timeout_seconds` would
    exceed `MAX_TOTAL_VERIFICATION_BUDGET_SECONDS`. In every case, NO
    subprocess for ANY command in the plan has been launched by the time
    this function raises or returns -- it never performs I/O beyond reading
    the in-memory `body` string it is given.
    """
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

    section = extract_verification_commands_section(body) or ""
    # Issue #2788 AC2: the SAME shared-parser-backed adapter normal execution
    # uses (`_command_entries_from_shared_parser()`), instead of the legacy
    # `extract_fenced_bash_blocks()` + `parse_commands_from_block()` grammar
    # -- so this plan's command occurrence set never drifts from what normal
    # execution actually runs. Issue #2788 AC3/AC13: an explanatory non-`$`
    # line inside a bash fence is a `VcParseResult.static_errors` entry, NOT
    # a `VcCommandEntry` -- it is therefore NEVER present in
    # `command_tuples` below and never becomes a command occurrence /
    # subprocess candidate in this plan, without this function needing to
    # raise any new exception type (fail-closed by omission, not by
    # exception -- preserving the existing caller-facing contract of
    # `contract_readiness_check.py` / `run_contract_review_once.py` /
    # `run_root_review_pipeline.py`, which are outside this Issue's Allowed
    # Paths).
    command_tuples, _parse_result = _command_entries_from_shared_parser(section)

    # Issue #2788 fix_delta P1-B: normal execution (`_main_impl()`) rejects
    # the WHOLE body -- zero subprocess candidates, `results: []`,
    # `status: blocked` -- the instant ANY `non_dollar_command` static
    # error is present anywhere in `_parse_result.static_errors`, even when
    # OTHER, individually well-formed `$ command` lines exist in the SAME
    # section (see the `_non_canonical_errors` whole-body-rejection branch
    # in `_main_impl()` below). Before this fix, THIS function ignored
    # `_parse_result` entirely and still built a command occurrence / timeout
    # budget candidate from every remaining well-formed `$ command` line --
    # drifting from what normal execution actually runs (violating AC2/AC3).
    # Discarding `command_tuples` here (rather than raising) preserves the
    # existing no-new-exception-type contract for
    # `contract_readiness_check.py` / `run_contract_review_once.py` /
    # `run_root_review_pipeline.py` (AC13, outside this Issue's Allowed
    # Paths) -- this plan simply converges on the SAME "reject by omission"
    # outcome (zero occurrences) normal execution's whole-body rejection
    # already produces.
    if any(se.kind == "non_dollar_command" for se in _parse_result.static_errors):
        command_tuples = []

    command_occurrence_count = 0
    launch_upper_bound = 0
    state_epoch = 0
    seen_in_epoch: Dict[int, set] = {}

    budgets_by_hash: Dict[str, Dict[str, Any]] = {}
    command_occurrences: List[Dict[str, Any]] = []
    aggregate_timeout_seconds = 0

    for entry in command_tuples:
        command = entry[1]
        command_occurrence_count += 1

        command_hash = compute_command_hash(command)
        if command_hash not in budgets_by_hash:
            budgets_by_hash[command_hash] = compute_command_timeout_budget(
                command,
                override_seconds=global_override_seconds,
                default_seconds=per_command_timeout_seconds,
                history_snapshot=history_snapshot,
                cwd=cwd,
                repo_root=repo_root,
            )
        _budget = budgets_by_hash[command_hash]
        aggregate_timeout_seconds += (
            _budget["timeout_seconds"] + _budget["cleanup_tail_seconds"]
        )

        is_pure = _is_parallel_eligible_command(command, cwd, allowed_paths)
        command_occurrences.append(
            {"command_hash": command_hash, "is_pure": is_pure}
        )
        if is_pure:
            bucket = seen_in_epoch.setdefault(state_epoch, set())
            key = command.strip()
            if key in bucket:
                # Executor dedup-replays this observation: not an
                # additional subprocess launch.
                continue
            bucket.add(key)
            launch_upper_bound += 1
        else:
            # Non-pure: never dedup-replayed, and advances the state
            # barrier so any later repeat of an earlier pure command
            # belongs to a new epoch (AC4).
            launch_upper_bound += 1
            state_epoch += 1

    if aggregate_timeout_seconds > MAX_TOTAL_VERIFICATION_BUDGET_SECONDS:
        raise AggregateTimeoutExceedsPolicyError(
            aggregate_timeout_seconds, MAX_TOTAL_VERIFICATION_BUDGET_SECONDS
        )

    command_budgets = list(budgets_by_hash.values())

    # Issue #2254 AC1: bind the snapshot's own digest (or `None` when no
    # snapshot was supplied) into the plan digest too -- this is additive
    # belt-and-braces on top of `command_budgets` already differing when
    # history evidence differs; it also makes an ABSENT vs PRESENT (but
    # empty/`store_status != "ok"`) snapshot distinguishable even in the
    # (degenerate) case where no command in this body has ANY history
    # record either way.
    _history_snapshot_digest = (
        history_snapshot.get("snapshot_digest") if history_snapshot is not None else None
    )
    envelope_for_digest = {
        "schema": CANONICAL_VC_PLAN_SCHEMA,
        "policy_version": CANONICAL_VC_PLAN_POLICY_VERSION,
        "body_sha256": body_sha256,
        "parser_contract_version": VC_PLAN_PARSER_CONTRACT_VERSION,
        "command_occurrence_count": command_occurrence_count,
        "launch_upper_bound": launch_upper_bound,
        "per_command_timeout_seconds": per_command_timeout_seconds,
        "max_workers": max_workers,
        "policy_cap": policy_cap,
        "command_occurrences": command_occurrences,
        "command_budgets": command_budgets,
        "aggregate_timeout_seconds": aggregate_timeout_seconds,
        "override_provenance": (
            "explicit_override" if global_override_seconds is not None else None
        ),
        "history_snapshot_digest": _history_snapshot_digest,
    }
    plan_digest = hashlib.sha256(_canonicalize_for_digest(envelope_for_digest)).hexdigest()

    return {
        "schema": CANONICAL_VC_PLAN_SCHEMA,
        "policy_version": CANONICAL_VC_PLAN_POLICY_VERSION,
        "body_sha256": body_sha256,
        "parser_contract_version": VC_PLAN_PARSER_CONTRACT_VERSION,
        "command_occurrence_count": command_occurrence_count,
        "launch_upper_bound": launch_upper_bound,
        "per_command_timeout_seconds": per_command_timeout_seconds,
        "max_workers": max_workers,
        "policy_cap": policy_cap,
        "command_occurrences": command_occurrences,
        "command_budgets": command_budgets,
        "aggregate_timeout_seconds": aggregate_timeout_seconds,
        # Kept as an alias of `plan_digest` for backward-compatible field
        # name (Issue #2233 AC1/AC6 originally introduced this name); both
        # are now the SAME full-envelope canonical-serialization hash
        # (fix_delta P1-3 -- previously `estimator_evidence_digest` only
        # hashed a sorted list of small per-distinct-command digests).
        "estimator_evidence_digest": f"sha256:{plan_digest}",
        "plan_digest": f"sha256:{plan_digest}",
        # Issue #2254 additive field: the history snapshot's own digest (or
        # `None`), surfaced so a caller can independently verify which
        # snapshot a given plan_digest was derived from.
        "history_snapshot_digest": _history_snapshot_digest,
    }


def verify_canonical_vc_plan_digest(plan: Dict[str, Any], expected_digest: Optional[str]) -> None:
    """Cross-process plan-identity verification (Issue #2233 fix_delta
    P0-1). Raises `VcPlanDigestMismatchError` (non-retryable) if
    `expected_digest` is given and does not match `plan["plan_digest"]`. A
    `None` `expected_digest` is a no-op (the caller did not request
    verification, e.g. a direct in-process caller that already holds the
    SAME `plan` object by reference)."""
    if expected_digest is None:
        return
    actual_digest = plan["plan_digest"]
    if actual_digest != expected_digest:
        raise VcPlanDigestMismatchError(expected_digest, actual_digest)


# ---------------------------------------------------------------------------
# History-based estimator wiring (Issue #2254)
# ---------------------------------------------------------------------------
#
# This section is the ONLY place `baseline_vc_preflight.py` talks to
# `vc_runtime_history.py`. `compute_command_timeout_budget()` /
# `compute_canonical_vc_plan()` above only ever consume an already-built
# `history_snapshot` dict -- they never call anything below directly.


def _resolve_git_common_dir(cwd: str) -> Optional[str]:
    """Resolve the REAL, shared `.git` common directory for `cwd` (Issue
    #2254: the history store is keyed by this, not by the worktree path,
    so every worktree of the SAME repository shares ONE history store).
    Returns `None` (never raises) if `cwd` is not inside a git working
    tree or `git` itself is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    if not os.path.isabs(raw):
        raw = os.path.join(cwd, raw)
    return os.path.realpath(raw)


def resolve_repo_root_for_history(cwd: str) -> Optional[str]:
    """Resolve the git WORKTREE ROOT (`git rev-parse --show-toplevel`) for
    `cwd` (Issue #2254 fix_delta P0 blocker 2/3, OWNER REQUEST_CHANGES
    source-repo PR 2382 review comment 5458281756):
    `command_group_key` must be keyed off a REPO-RELATIVE cwd, not an
    absolute one, so history samples recorded from one worktree of a
    repository are shared by every OTHER worktree of the SAME repository
    (each worktree has a DIFFERENT absolute path, but its own
    `show-toplevel` IS its own cwd whenever VCs are run from the worktree
    root, so both worktrees' `compute_command_group_key()` calls normalize
    to the SAME relative "." cwd component).

    Deliberately NOT `_resolve_git_common_dir()` (used ONLY to key the
    SHARED SQLite STORE FILE itself, which for every worktree of a
    repository points at the ONE original repository's `.git` -- conflating
    the two would make every worktree's `command_group_key` collapse onto
    the SAME repo_root REGARDLESS of which worktree cwd was actually used,
    defeating the point of a repo-RELATIVE cwd).

    A pure function of `cwd` (no caching): calling this twice with the
    SAME `cwd` string always returns the SAME value, which is what lets a
    PRODUCER (`produce_immutable_history_snapshot()`) and a CONSUMER
    (`compute_canonical_vc_plan()`/`compute_command_timeout_budget()`) --
    even in SEPARATE processes across a subprocess boundary -- converge on
    the SAME `command_group_key` as long as they are given the SAME `cwd`
    value, without needing any explicit cross-process handoff of
    `repo_root` itself.

    Returns `None` (never raises) if `cwd` is not inside a git working tree
    or `git` itself is unavailable; callers pass `None` straight through to
    `compute_command_group_key(..., repo_root=None)`, which degrades to its
    pre-fix_delta absolute-cwd behavior for that one command (a narrower,
    non-shared -- but still internally self-consistent -- history bucket)."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return os.path.realpath(raw)


def _distinct_command_texts_from_body(body: str) -> List[str]:
    """Every DISTINCT (first-occurrence-ordered) VC command text in
    `body`'s `## Verification Commands` section -- the same population
    `compute_canonical_vc_plan()` computes one `command_timeout_budget/v1`
    entry for.

    Issue #2788 AC12: sources this population from the SAME shared-parser
    adapter (`_command_entries_from_shared_parser()`) `compute_canonical_vc_plan()`
    and normal execution use, instead of independently re-deriving it from
    the legacy `extract_fenced_bash_blocks()` + `parse_commands_from_block()`
    grammar -- keeping this docstring's claim actually true.

    Issue #2788 fix_delta P1-B: for a body containing a `non_dollar_command`
    static error (an explanatory non-`$` line inside a bash fence), normal
    execution and `compute_canonical_vc_plan()` both reject the WHOLE body
    (zero commands) -- see `compute_canonical_vc_plan()`'s own P1-B
    docstring note. This function converges on the SAME empty population
    for the SAME reason, so its AC12 claim above ("the SAME command
    population `compute_canonical_vc_plan()` ... use") stays true even for
    a mixed body that has both a non-`$` line AND an otherwise well-formed
    `$ command` line.
    """
    section = extract_verification_commands_section(body) or ""
    command_tuples, _parse_result = _command_entries_from_shared_parser(section)
    if any(se.kind == "non_dollar_command" for se in _parse_result.static_errors):
        return []
    seen: set = set()
    ordered: List[str] = []
    for entry in command_tuples:
        command = entry[1]
        key = command.strip()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(command)
    return ordered


def _command_family(command: str) -> str:
    try:
        argv = shlex.split(command.strip())
    except ValueError:
        argv = command.strip().split()
    return argv[0] if argv else ""


def produce_immutable_history_snapshot(
    body: str,
    *,
    cwd: str = ".",
    max_seconds: int = MAX_PER_COMMAND_TIMEOUT_SECONDS,
    repo_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Root-owned, SINGLE read of the local per-repository history store
    for every DISTINCT command text in `body` (Issue #2254 AC1). Callers
    (this module's own `_main_impl()`, and additive wiring in
    `contract_readiness_check.py` / `run_contract_review_once.py` /
    `run_root_review_pipeline.py`) call this EXACTLY ONCE per
    canonical-VC-plan computation and pass the resulting dict, unchanged,
    into every `compute_canonical_vc_plan(..., history_snapshot=...)` call
    for that SAME plan (including across a subprocess boundary, via
    `vc_runtime_history.write_history_snapshot_file()` /
    `--history-snapshot-file`).

    NEVER raises. Degrades to `vc_runtime_history.empty_history_snapshot(...)`
    (Issue #2254 AC8) if the git common dir cannot be resolved (e.g. `cwd`
    is outside any git working tree) or the store itself is
    missing/locked/corrupt/unknown-schema.

    Test-safety guard: when running under pytest (`PYTEST_CURRENT_TEST`
    set) WITHOUT an explicit `VC_RUNTIME_HISTORY_STORE_PATH` override, this
    returns `None` (the SAME value pre-#2254 callers implicitly used, and
    the SAME value `compute_canonical_vc_plan(history_snapshot=None)`
    already treats as "no history evidence at all") rather than touching
    the real developer/CI machine's default `$XDG_STATE_HOME` store -- the
    30+ existing test files across this repo that invoke
    `baseline_vc_preflight.py` were never designed to expect a real
    out-of-repo filesystem write. Returning `None` here (rather than a
    real-but-empty `store_status` snapshot dict) is deliberate: a non-null
    snapshot object carries its own `snapshot_digest`, which would get
    baked into `plan_digest` via `history_snapshot_digest` and diverge
    from every comparison plan a PRE-#2254 test computes by calling
    `compute_canonical_vc_plan()` with no `history_snapshot` argument at
    all (which defaults to `None`) -- breaking `plan_digest` convergence
    between this executor and its callers under test, even though no
    Issue #2254 behavior was otherwise exercised. Issue #2254's own tests
    all set `VC_RUNTIME_HISTORY_STORE_PATH` explicitly to exercise the
    real read/write path hermetically instead of relying on this guard.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "VC_RUNTIME_HISTORY_STORE_PATH"
    ):
        return None
    pairs: List[Tuple[str, str]] = []
    for command in _distinct_command_texts_from_body(body):
        # Issue #2254 fix_delta P0 blocker 2 (OWNER REQUEST_CHANGES
        # source-repo PR 2382 review comment 5458281756):
        # `repo_root` (when the caller resolved one via
        # `resolve_repo_root_for_history(cwd)`) must be threaded into the
        # SAME key computation `compute_command_timeout_budget()`'s LOOKUP
        # uses, so producer and consumer agree on where evidence lives.
        group_key = _vc_runtime_history.compute_command_group_key(
            command, cwd, repo_root=repo_root
        )
        fingerprint = _vc_runtime_history.compute_environment_fingerprint(
            _command_family(command)
        )
        pairs.append((group_key, fingerprint))

    git_common_dir = _resolve_git_common_dir(cwd)
    if git_common_dir is None:
        return _vc_runtime_history.empty_history_snapshot("missing")
    store_path = _vc_runtime_history.resolve_store_path(git_common_dir)
    return _vc_runtime_history.build_history_snapshot(store_path, pairs, max_seconds=max_seconds)


def record_command_execution_sample(
    command: str,
    cwd: str,
    *,
    exit_code: Optional[int],
    stderr: str,
    duration_ms: Optional[int],
    applied_timeout_seconds: int,
    repo_root: Optional[str] = None,
) -> None:
    """Best-effort, NEVER-raising history sample recording for ONE REAL
    subprocess launch (Issue #2254 AC6: one launch -> one `record_sample()`
    call -> at most one stored row; AC7: non-blocking; AC8: a recording
    failure must NEVER affect VC classification/exit code -- this function
    swallows every exception).

    Maps `run_command()`'s return shape to a `status`: exit_code == 0 ->
    `success`; exit_code == -1 with `stderr == "timeout"` -> `timed_out`
    (Issue #2254 In Scope: right-censored, NOT excluded); anything else ->
    `failed` (diagnostic-only, excluded from the duration percentile).

    Test-safety guard: same as `produce_immutable_history_snapshot()`
    above -- a no-op under pytest without an explicit
    `VC_RUNTIME_HISTORY_STORE_PATH` override.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "VC_RUNTIME_HISTORY_STORE_PATH"
    ):
        return
    try:
        git_common_dir = _resolve_git_common_dir(cwd)
        if git_common_dir is None:
            return
        store_path = _vc_runtime_history.resolve_store_path(git_common_dir)
        # Issue #2254 fix_delta P0 blocker 2: same repo_root-threaded key
        # as the snapshot producer/lookup above, so a WRITE from this
        # worktree lands under the SAME bucket a READ from any other
        # worktree of the SAME repository will look up.
        group_key = _vc_runtime_history.compute_command_group_key(
            command, cwd, repo_root=repo_root
        )
        fingerprint = _vc_runtime_history.compute_environment_fingerprint(
            _command_family(command)
        )
        if exit_code == 0:
            status = "success"
        elif exit_code == -1 and stderr == "timeout":
            status = "timed_out"
        else:
            status = "failed"
        _vc_runtime_history.record_sample(
            store_path,
            execution_id=_vc_runtime_history.new_execution_id(),
            command_group_key=group_key,
            environment_fingerprint=fingerprint,
            status=status,
            command_hash=compute_command_hash(command),
            duration_ms=(
                max(0, int(duration_ms)) if isinstance(duration_ms, int) else None
            ),
            applied_timeout_ms=int(applied_timeout_seconds) * 1000,
        )
    except Exception:
        # Issue #2254 AC8: the WRITE path degrades exactly like the READ
        # path -- never allowed to affect the VC's own classification or
        # this process's exit code.
        return



# ---------------------------------------------------------------------------
# Duplicate non-pure VC command diagnostics (Issue #2232)
# ---------------------------------------------------------------------------
#
# `vc_duplicate_diagnostic_report/v1` is a diagnostic-only report,
# INDEPENDENT of the canonical execution plan above (`command_occurrence_count`
# / `launch_upper_bound` / `status` / `results` / exit code are never
# changed by anything in this section -- Issue #2232 Outcome). It detects
# the SAME non-pure (state-barrier) VC command text occurring more than
# once in a body's `## Verification Commands` section, which is USUALLY an
# accidental duplication (copy/paste) rather than an intentional re-run.
#
# Pure (dedup-eligible) commands (`_is_parallel_eligible_command()`) are
# NEVER flagged: the canonical plan already treats their repetition as safe
# to dedup-replay, so a diagnostic warning would be pure noise (Issue #2232
# In Scope, positive/negative control).
#
# Scope semantics fixed by Issue #2232 AC6 (each is a deliberate, tested
# design decision -- NOT an incidental default):
#
#   - `preflight-scope: pr_review_only` / `preflight-scope: runtime_only`
#     commands ARE included. The scope marker only changes WHICH execution
#     phase actually runs the command; it does not change the fact that the
#     same non-pure command text appears twice in the body, so the
#     duplication signal is still meaningful within that phase.
#   - `baseline-expect: deferred` commands are EXCLUDED. `baseline_vc_preflight`
#     intentionally never executes a `deferred` VC, so two `deferred`
#     occurrences never race/duplicate a real subprocess launch; flagging
#     them would be a false positive against a command the preflight never
#     runs at all.
#   - Commands that `classify_static_command()` statically blocks (e.g.
#     unsupported shell syntax, compound commands, denied commands) are
#     EXCLUDED: a statically-blocked command never reaches subprocess
#     execution, so duplicate occurrences of it can never duplicate a real
#     launch.
#   - A state-changing (non-pure) command interposed between two
#     occurrences of an otherwise-identical non-pure command does NOT
#     suppress the diagnostic: this report is a body-text-level duplication
#     signal, independent of the canonical plan's `state_epoch` barrier
#     bookkeeping (which only affects whether a PURE command may be
#     dedup-replayed, not whether a NON-pure duplicate is worth flagging).

VC_DUPLICATE_DIAGNOSTIC_SCHEMA = "vc_duplicate_diagnostic_report/v1"
VC_DUPLICATE_DIAGNOSTIC_RULE_ID = "VCP_DUPLICATE_NON_PURE_COMMAND"
VC_DUPLICATE_DIAGNOSTIC_LEVEL = "warning"


def not_computed_diagnostic_report() -> Dict[str, Any]:
    """Issue #2232 AC3: `status: not_computed` distinguishes "diagnosed and
    found zero duplicates" (`status: complete`, `items: []`) from "never
    diagnosed" (this function) -- used by every early-return `main()` path
    that exits BEFORE the body/canonical plan is obtained (retrieval
    failure, current-head validation failure, VC section/command
    extraction failure, budget/digest policy rejection)."""
    return {
        "schema": VC_DUPLICATE_DIAGNOSTIC_SCHEMA,
        "status": "not_computed",
        "items": [],
    }


def compute_duplicate_diagnostic_report(
    body: str,
    canonical_plan_digest: Optional[str],
    cwd: str,
    allowed_paths: List[str],
) -> Dict[str, Any]:
    """Issue #2232: compute the `vc_duplicate_diagnostic_report/v1` for
    `body`, bound to `body_sha256`, the canonical plan's `plan_digest`
    (`canonical_plan_digest`, for pinning context only -- never fed back
    into the canonical plan's OWN digest computation, Issue #2232 Out of
    Scope), and the classification context (`cwd` / `allowed_paths`) used
    for pure/non-pure convergence (AC2).

    Deterministic (AC5): re-running this function against the SAME `body`
    and the SAME classification context always returns byte-identical
    `items[]`, ordered by first-occurrence ordinal, tie-broken by
    `command_identity_hash`.
    """
    section = extract_verification_commands_section(body) or ""
    blocks = extract_fenced_bash_blocks(section)
    resolved_allowed_paths = list(allowed_paths or [])
    resolved_cwd = Path(cwd)

    ordinal = 0
    groups: Dict[str, List[Dict[str, Any]]] = {}

    for block_index, block in enumerate(blocks):
        for entry in parse_commands_from_block(block):
            (
                ac_label,
                command,
                line_in_block,
                _preflight_scope,
                _vc_regex_intent,
                baseline_expect,
                _vc_role,
                _annotation_line_no,
                _annotation_raw,
            ) = entry
            ordinal += 1

            # Pure (dedup-eligible) commands are never flagged (AC2 convergence).
            if _is_parallel_eligible_command(command, cwd, resolved_allowed_paths):
                continue
            # `baseline-expect: deferred` commands are never executed by
            # baseline preflight (AC6).
            if baseline_expect == "deferred":
                continue
            # Statically-blocked commands never reach subprocess execution (AC6).
            if classify_static_command(
                command, resolved_cwd, allowed_paths=resolved_allowed_paths
            ) is not None:
                continue

            command_identity_hash = compute_command_hash(command)
            groups.setdefault(command_identity_hash, []).append(
                {
                    "ordinal": ordinal,
                    "ac_label": ac_label,
                    "block_index": block_index,
                    "line_in_block": line_in_block,
                    "command": command,
                }
            )

    items: List[Dict[str, Any]] = []
    for command_identity_hash, occurrences in groups.items():
        if len(occurrences) < 2:
            continue
        items.append(
            {
                "rule_id": VC_DUPLICATE_DIAGNOSTIC_RULE_ID,
                "level": VC_DUPLICATE_DIAGNOSTIC_LEVEL,
                "message": (
                    f"Non-pure VC command occurs {len(occurrences)} times in "
                    "the Verification Commands section: "
                    f"{occurrences[0]['command']!r}"
                ),
                "command_identity_hash": command_identity_hash,
                "occurrence_count": len(occurrences),
                "occurrences": [
                    {
                        "ordinal": occ["ordinal"],
                        "ac_label": occ["ac_label"],
                        "block_index": occ["block_index"],
                        "line_in_block": occ["line_in_block"],
                    }
                    for occ in occurrences
                ],
                "_first_ordinal": occurrences[0]["ordinal"],
            }
        )

    items.sort(key=lambda item: (item["_first_ordinal"], item["command_identity_hash"]))
    for item in items:
        del item["_first_ordinal"]

    return {
        "schema": VC_DUPLICATE_DIAGNOSTIC_SCHEMA,
        "status": "complete",
        "body_sha256": f"sha256:{compute_source_hash(body)}",
        "canonical_plan_digest": canonical_plan_digest,
        "classification_context": {
            "cwd": cwd,
            "allowed_paths": resolved_allowed_paths,
        },
        "items": items,
    }


# `detect_compound_command()` moved to `vc_contract_syntax.py` (Issue #2788
# AC9): it is now the SINGLE quote-aware compound-shell detection primitive
# shared by this module's own classification call sites (imported above)
# AND `vc_contract_syntax.parse_verification_commands_section()`'s static
# `compound_shell` check -- no independent regex-based compound detector
# remains anywhere in this grammar.


# ---------------------------------------------------------------------------
# SIGTERM cooperative cancellation (Issue #2207 AC9)
# ---------------------------------------------------------------------------
#
# `run_command()` below already kills an INDIVIDUAL VC subprocess's whole
# process group on ITS OWN per-command timeout (`_kill_process_group()`).
# This section adds a SEPARATE, process-wide layer: while any VC
# subprocess is in flight, its process group id is tracked in
# `_ACTIVE_PROCESS_GROUPS`. If THIS `baseline_vc_preflight.py` process
# itself receives SIGTERM (e.g. a future outer aggregate-deadline wrapper
# terminating it because the aggregate budget was exceeded), the installed
# handler reaps every CURRENTLY active VC process group (direct child +
# grandchildren, since each was launched with `start_new_session=True`)
# before this process exits, so no VC descendant is left orphaned.

_ACTIVE_PROCESS_GROUPS_LOCK = threading.Lock()
_ACTIVE_PROCESS_GROUPS: set = set()
# Issue #2207 OWNER P1-2 item 5 (fault-injection-discovered fix): pgid ->
# the Popen handle for the DIRECT child of THIS process in that group.
# `os.killpg(pgid, SIGTERM/SIGKILL)` makes the process exit at the kernel
# level, but without an explicit `.wait()`/`.poll()` from ITS actual
# parent (this process), the direct child becomes a zombie that
# `os.killpg(pgid, 0)` / `os.kill(pid, 0)` still report as "alive"
# (a zombie still occupies its pid/pgid slot until reaped) -- indefinitely,
# if nothing ever calls `wait()` on it. `reap_all_active_process_groups()`
# uses this mapping to explicitly reap the direct child it owns after
# signaling (grandchildren are not this process's own children and are
# reaped by their own eventual re-parenting once the direct child exits,
# which the OS handles once the direct child itself is gone).
_ACTIVE_PROCESS_HANDLES: Dict[int, "subprocess.Popen"] = {}

# Issue #2207 OWNER P0-2 (PR #2221 REQUEST_CHANGES): a module-level flag
# only, mutated by the signal handler with a plain attribute assignment
# (atomic under the GIL, no lock needed). Read/cleared only from ordinary
# control flow (never from inside the handler itself).
_CANCEL_REQUESTED = False


class CooperativeCancellationRequested(BaseException):
    """Raised BY the SIGTERM handler (never caught there) so that PEP 475
    ("interrupted system calls are retried") does NOT silently swallow the
    signal while the main thread is blocked inside `Popen.communicate()`.
    Deriving from `BaseException` (not `Exception`) is deliberate: ordinary
    `except Exception` clauses inside `run_command()` must NOT intercept
    this and must let it propagate all the way to a single top-level
    handler in `main()`, where the ACTUAL registry snapshot + TERM/KILL +
    wait work happens in normal control flow -- never inside the signal
    handler (Issue #2207 OWNER P0-2: a signal handler that acquires
    `_ACTIVE_PROCESS_GROUPS_LOCK` can deadlock the single main thread if
    the signal lands while that same thread already holds the lock inside
    `_register_active_process_group()` / `_unregister_active_process_group()`,
    since `threading.Lock` is non-reentrant and no other thread can ever
    release it for a single-threaded program)."""


def _register_active_process_group(
    pid: int, process: Optional["subprocess.Popen"] = None
) -> Optional[int]:
    """Register `pid`'s process group as active and return the PGID token
    that was registered (or `None` if the pid could not be resolved).

    Issue #2207 OWNER P1-1: the caller MUST hold onto this returned token
    and pass it back to `_unregister_active_process_group()` verbatim
    rather than re-deriving a PGID from the PID at unregister time -- by
    the time a process is unregistered it has typically already been
    waited/reaped, so `os.getpgid(pid)` reliably raises `ProcessLookupError`
    and unregistration would silently no-op, leaving a stale PGID in the
    registry forever (a correctness hazard, not just a leak: Linux PIDs
    wrap at `pid_max` and a stale PGID can be reassigned to an unrelated
    process group that a later SIGTERM sweep would then kill).

    `process` (Issue #2207 OWNER P1-2 item 5 fault-injection-discovered
    fix), if given, is the `Popen` handle for THIS process's own direct
    child in that group; it is kept so `reap_all_active_process_groups()`
    can explicitly `.wait()` it (avoiding a zombie -- `os.killpg`/`os.kill`
    signal-0 liveness checks still report a zombie as "alive" until its
    actual parent reaps it)."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return None
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        _ACTIVE_PROCESS_GROUPS.add(pgid)
        if process is not None:
            _ACTIVE_PROCESS_HANDLES[pgid] = process
    return pgid


def _unregister_active_process_group(pgid: Optional[int]) -> None:
    """Remove a previously-registered PGID token from the active-groups
    registry. `pgid` MUST be the exact value `_register_active_process_group()`
    returned (Issue #2207 OWNER P1-1) -- never re-resolved from a PID."""
    if pgid is None:
        return
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        _ACTIVE_PROCESS_GROUPS.discard(pgid)
        _ACTIVE_PROCESS_HANDLES.pop(pgid, None)


def _reap_owned_handle_if_present(pgid: int) -> None:
    """Best-effort, non-blocking: if THIS process registered a `Popen`
    handle for `pgid` (its own direct child in that group), reap it now if
    it has already exited (Issue #2207 OWNER P1-2 item 5
    fault-injection-discovered fix: signaling alone does not reap;
    grandchildren are not this process's own children and are left to
    re-parenting, which only proceeds once the direct child itself is
    actually reaped, not merely killed)."""
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        handle = _ACTIVE_PROCESS_HANDLES.get(pgid)
    if handle is None:
        return
    try:
        handle.wait(timeout=0)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass


def _process_group_is_absent(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, OSError):
        return True
    return False


def reap_all_active_process_groups(*, grace_seconds: float = 5.0, poll_interval: float = 0.05) -> int:
    """Terminate + reap every VC process group currently registered as
    active.

    Issue #2207 OWNER P0-2 fix: this function is called from ORDINARY
    control flow only (a top-level `except CooperativeCancellationRequested`
    in `main()`), never from inside the signal handler itself, so taking
    `_ACTIVE_PROCESS_GROUPS_LOCK` here cannot deadlock the (single) main
    thread the way taking it from inside the handler could.

    Issue #2207 OWNER P0-2 (ordering) fix: SIGTERM is sent to EVERY active
    group FIRST, then a SINGLE global grace period is awaited (not one
    grace period PER group serially -- with up to `MAX_VC_EXECUTION_SLOTS`
    (40) groups, a naive per-group `wait(timeout=5)` loop could take
    minutes), and only groups still alive after that single grace window
    are SIGKILLed.

    Best-effort/idempotent: a group that already exited between
    registration and this call is not an error. Returns the number of
    groups that were signaled (not necessarily still alive).
    """
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        pgids = list(_ACTIVE_PROCESS_GROUPS)

    signaled: List[int] = []
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            _unregister_active_process_group(pgid)
            continue
        signaled.append(pgid)

    still_alive = set(signaled)
    deadline = time.monotonic() + grace_seconds
    while still_alive and time.monotonic() < deadline:
        for pgid in list(still_alive):
            # Issue #2207 OWNER P1-2 item 5 (fault-injection-discovered
            # fix): reap this process's own direct child in the group FIRST
            # -- a zombie direct child still answers `killpg(pgid, 0)` as
            # "alive", so `_process_group_is_absent()` below would never
            # observe absence for a group whose only remaining member is
            # our own unreaped (but already-exited) direct child.
            _reap_owned_handle_if_present(pgid)
            if _process_group_is_absent(pgid):
                still_alive.discard(pgid)
                _unregister_active_process_group(pgid)
        if still_alive:
            time.sleep(poll_interval)

    for pgid in still_alive:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    # Issue #2207 OWNER P1-2 item 5 (fault-injection-discovered fix): after
    # SIGKILL, bounded-poll (reusing the same grace window shape as
    # `_kill_process_group()`) so the direct child actually gets reaped
    # (and the group's absence actually gets confirmed) before this
    # function returns, instead of returning immediately after sending
    # SIGKILL with no confirmation.
    kill_deadline = time.monotonic() + grace_seconds
    while still_alive and time.monotonic() < kill_deadline:
        for pgid in list(still_alive):
            _reap_owned_handle_if_present(pgid)
            if _process_group_is_absent(pgid):
                still_alive.discard(pgid)
        if still_alive:
            time.sleep(poll_interval)

    for pgid in signaled:
        _unregister_active_process_group(pgid)

    return len(signaled)


def _sigterm_cooperative_handler(signum: int, frame: Any) -> None:  # noqa: ANN401
    """Issue #2207 OWNER P0-2: the ENTIRE body of a SIGTERM handler must be
    limited to setting a plain flag and raising a dedicated exception --
    NO lock acquisition, NO sleep, NO process wait, NO kill here. Raising
    `CooperativeCancellationRequested` (a `BaseException`) is what actually
    unwinds the main thread out of a blocking `Popen.communicate()` call
    (PEP 475 only auto-retries an interrupted syscall when the handler
    returns normally; raising instead propagates the exception through the
    call stack), so the REAL cleanup work happens afterwards in ordinary
    control flow inside `main()`."""
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    raise CooperativeCancellationRequested()


def install_sigterm_cooperative_cancellation() -> None:
    """Install a SIGTERM handler that requests cooperative cancellation
    (Issue #2207 AC9). POSIX-only (relies on `os.killpg` / process-group
    semantics); a caller on a platform without them should NOT call this
    (the caller is expected to treat that as `environment blocked`, not
    silently skip cancellation)."""
    signal.signal(signal.SIGTERM, _sigterm_cooperative_handler)


def posix_process_groups_supported() -> bool:
    """Issue #2207 AC9: whether this environment provides POSIX process
    group semantics (`os.killpg` / `start_new_session`). If False, the
    caller must treat SIGTERM cooperative cancellation as unavailable
    (`environment blocked`), never silently degrade to a no-op success."""
    return os.name == "posix" and hasattr(os, "killpg") and hasattr(signal, "SIGTERM")


def run_command(command: str, timeout_seconds: int, cwd: str) -> Tuple[int, str, str, int, Dict[str, str]]:
    """
    コマンドを単体実行。

    戻り値: (exit_code, stdout, stderr, duration_ms, runner_env_delta)
    """
    try:
        # shlex.split で argv を構築（shell=False で安全に実行）
        argv = shlex.split(command)
    except ValueError:
        # shlex.split に失敗した場合は compound command の可能性
        return -1, "", "shlex.split failed", 0, {}

    pnpm_evidence = None
    if pnpm_gate_registry.looks_like_pnpm(argv):
        launch_argv, pnpm_evidence, gate_error = pnpm_gate_registry.prepare_launch(argv, cwd)
        if gate_error or launch_argv is None:
            return -1, "", gate_error or "pnpm_gate:launch_preparation_failed", 0, {}
        argv = launch_argv
        env_delta = dict(pnpm_evidence["runner_env_delta"])
    else:
        env_delta = _fixed_env_delta_for_argv(argv)
    run_env = None
    if env_delta:
        run_env = os.environ.copy()
        # Issue #2638 P2 fix (PR #2643 review Finding 3): a `_ENV_UNSET_SENTINEL`
        # value means "delete this key from the subprocess env" (e.g. rg's
        # `RIPGREP_CONFIG_PATH` isolation), not "set it to that literal
        # string". Ordinary string values are applied as before. This only
        # ever mutates the COPY made above, never `os.environ` itself.
        for _env_key, _env_value in env_delta.items():
            if _env_value == _ENV_UNSET_SENTINEL:
                run_env.pop(_env_key, None)
            else:
                run_env[_env_key] = _env_value

    start = datetime.now()
    process: Optional[subprocess.Popen] = None
    try:
        # Issue #2165 P1-2 (OWNER 2026-08-15 REQUEST_CHANGES merge condition
        # #5, PR #2177 fix_delta iteration 3): the VC subprocess is now
        # launched via `Popen(..., start_new_session=True)` instead of the
        # previous `subprocess.run(..., timeout=...)`. Python's official
        # `subprocess.run(timeout=...)` contract only guarantees killing the
        # DIRECT child on timeout, not any grandchild the VC command itself
        # may have spawned. `start_new_session=True` puts this subprocess in
        # its own new session (pgid == pid), independent of any other
        # concurrently-running VC subprocess spawned via the
        # `ThreadPoolExecutor(max_workers>1)` bounded-parallel executor
        # below (each Popen call creates its own session id -- there is no
        # shared/global pgid state, so this is safe under concurrency by
        # construction). On timeout, `_kill_process_group()` below signals
        # the ENTIRE process group (this VC's direct child AND any
        # grandchildren it spawned), not just the direct child.
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            shell=False,
            env=run_env,
            # Issue #1338 P0-2 fix (PR #1508 review): explicit stdin=DEVNULL.
            # subprocess.run() otherwise defaults to inheriting the parent's
            # stdin, which is a race hazard when multiple VC subprocesses
            # (e.g. two `rg` invocations) may run concurrently under the
            # bounded-parallel executor.
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Issue #2207 AC9: track this VC subprocess's process group while
        # it is in flight so a SIGTERM to the OUTER `baseline_vc_preflight.py`
        # process itself (see `install_sigterm_cooperative_cancellation()`)
        # can reap it even though this specific per-command timeout path
        # hasn't fired. `getattr(process, "pid", None)`: some existing test
        # doubles stand in for `subprocess.Popen` without a `.pid` attribute
        # (they never exercise the timeout/SIGTERM path); registration is a
        # best-effort side channel and must not raise for those.
        #
        # Issue #2207 OWNER P1-1: `_register_active_process_group()` returns
        # the PGID TOKEN it registered; that exact token (never a PID
        # re-resolved later) is what gets passed to
        # `_unregister_active_process_group()` below.
        _vc_process_pid = getattr(process, "pid", None)
        _vc_group_token: Optional[int] = None
        if _vc_process_pid is not None:
            _vc_group_token = _register_active_process_group(_vc_process_pid, process)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            # Drain pipes after the group is dead so `communicate()` doesn't
            # block indefinitely on an orphaned pipe write-end still held
            # open by a reaped-but-not-yet-reclaimed grandchild.
            try:
                process.communicate(timeout=5)
            except Exception:
                pass
            duration_ms = int((datetime.now() - start).total_seconds() * 1000)
            _unregister_active_process_group(_vc_group_token)
            return -1, "", "timeout", duration_ms, env_delta
        except CooperativeCancellationRequested:
            # Issue #2207 OWNER P0-2: propagate WITHOUT unregistering -- the
            # process group is still (as far as we know) alive; a top-level
            # handler in `main()` calls `reap_all_active_process_groups()` in
            # ordinary control flow, which needs this PGID to still be in
            # the registry to reap it.
            raise
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        _unregister_active_process_group(_vc_group_token)
        return process.returncode, stdout, stderr, duration_ms, env_delta
    except CooperativeCancellationRequested:
        raise
    except subprocess.TimeoutExpired:
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return -1, "", "timeout", duration_ms, env_delta
    except Exception as e:
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return -1, "", str(e), duration_ms, env_delta


def _kill_process_group(
    process: "subprocess.Popen", *, grace_seconds: float = 5.0, poll_interval: float = 0.05
) -> None:
    """
    Reap an entire process group spawned with `start_new_session=True`.

    Issue #2165 P1-2 (OWNER merge condition #5): on VC subprocess timeout,
    kill the WHOLE process group (direct child + any grandchildren it
    spawned), not merely the direct child pid. `start_new_session=True`
    guarantees `os.getpgid(process.pid) == process.pid` for this process
    (it is its own session/group leader), so this only ever targets the
    group this specific Popen call created -- never a shared/global group,
    and never another concurrently-running VC subprocess's group.

    Issue #2207 OWNER P0-3 fix: the group LEADER exiting (`process.wait()`
    returning after the SIGTERM grace period) does NOT imply the whole
    process GROUP is gone -- a grandchild that ignores SIGTERM survives
    independently once the leader has exited, and the previous
    implementation `return`ed as soon as `process.wait()` succeeded,
    leaving such a grandchild running forever (SIGKILL was only ever sent
    to the leader's own wait path, never to the group as a fallback after
    leader-exit). This version ALWAYS explicitly probes group membership
    with `os.killpg(pgid, 0)` after the leader wait returns (successfully
    or not) and unconditionally SIGKILLs the group (not just the leader)
    if anything is still alive, then bounded-polls until the group is
    confirmed absent.
    """
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, OSError):
        return  # group (leader + any grandchildren) already absent

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(poll_interval)


class SupervisedSubprocessResult:
    """Result of `run_subprocess_with_cooperative_supervisor()`. Shaped
    similarly to `subprocess.CompletedProcess` plus a `timed_out` flag
    (Issue #2207 OWNER P0-1)."""

    __slots__ = ("returncode", "stdout", "stderr", "timed_out", "duration_seconds")

    def __init__(
        self, returncode: int, stdout: str, stderr: str, timed_out: bool, duration_seconds: float
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration_seconds = duration_seconds


def run_subprocess_with_cooperative_supervisor(
    argv: List[str],
    *,
    timeout_seconds: float,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    grace_seconds: float = 5.0,
    poll_interval: float = 0.05,
) -> SupervisedSubprocessResult:
    """Issue #2207 OWNER P0-1 (PR #2221 REQUEST_CHANGES): a cooperative
    replacement for `subprocess.run(argv, timeout=timeout_seconds)`.

    CPython's `subprocess.run(timeout=...)` sends `process.kill()`
    (SIGKILL) DIRECTLY to the direct child on timeout -- this bypasses any
    SIGTERM handler the child installs (e.g. `baseline_vc_preflight.py`'s
    own `install_sigterm_cooperative_cancellation()`) entirely, and (since
    the child is isolated into its OWN process group via
    `start_new_session=True`) never reaches any VC descendant process
    group the child itself is tracking. `contract_readiness_check.py`'s
    `run_baseline_vc_preflight()` and `run_root_review_pipeline.py`'s
    `run_contract_readiness_check()` both call THIS function instead so
    the direct child gets a real chance to run its own cooperative cleanup
    before being forcibly killed.

    Behavior:
    1. Launches `argv` in its OWN process group (`start_new_session=True`),
       so this supervisor can signal the whole group, not just the leader.
    2. On `timeout_seconds` elapsing, sends SIGTERM to the child's process
       GROUP (never SIGKILL first).
    3. Waits a SINGLE bounded `grace_seconds` for the child to exit on its
       own (giving its own cooperative cleanup, if any, time to run).
    4. Only after that grace period does it send SIGKILL to the group.
    5. Confirms the process group is actually gone (bounded poll) before
       returning, then drains stdout/stderr.

    Float `timeout_seconds` is intentional (Issue #2207 OWNER P1-2 item 7):
    callers under test inject sub-second deadlines (e.g. 0.15s) to
    exercise the OUTER deadline path without waiting on realistic
    production-scale timeouts.
    """
    start = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        shell=False,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        pgid: Optional[int] = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        duration = time.monotonic() - start
        return SupervisedSubprocessResult(process.returncode, stdout, stderr, False, duration)
    except subprocess.TimeoutExpired:
        pass

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pgid = None

    if pgid is not None:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        if not _process_group_is_absent(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline and not _process_group_is_absent(pgid):
                time.sleep(poll_interval)
    else:
        try:
            process.kill()
        except OSError:
            pass

    try:
        stdout, stderr = process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            stdout, stderr = process.communicate(timeout=1)
        except Exception:
            stdout, stderr = "", ""

    duration = time.monotonic() - start
    returncode = process.returncode if process.returncode is not None else -1
    return SupervisedSubprocessResult(returncode, stdout or "", stderr or "", True, duration)


def _pnpm_gate_evidence_for_command(command: str, cwd: str) -> Optional[Dict[str, Any]]:
    """Produce consumer-verifiable gate evidence without a fallback pathway."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    evidence, error = pnpm_gate_registry.evidence_for_request(argv, cwd)
    return evidence if error is None else None


def truncate_line_bytes(line: str, max_bytes: int) -> str:
    """
    単一行を byte 単位で切り詰める。

    戻り値: byte で切り詰められた行（UTF-8 safe）
    """
    raw = line.encode("utf-8")
    if len(raw) <= max_bytes:
        return line
    return raw[:max_bytes].decode("utf-8", errors="replace")


def truncate_output(text: str, max_lines: int = 20, bytes_per_line: int = 2048) -> Tuple[List[str], bool, int]:
    """
    stdout / stderr を行数とバイト数で切り詰める。

    中リスク 3: truncation 情報を返す。
    戻り値: (lines, was_truncated, original_line_count)
      - lines: truncate されたテキストを行配列で返す（空の場合は []）
      - was_truncated: 行数が max_lines を超えたかどうか
      - original_line_count: 元のテキストの行数
    """
    all_lines = text.split("\n")
    original_line_count = len(all_lines)
    lines = all_lines[:max_lines]
    was_truncated = original_line_count > max_lines

    result = []
    for line in lines:
        # B7: byte-safe truncation
        truncated_line = truncate_line_bytes(line, bytes_per_line)
        if truncated_line or line:  # 中リスク 3: 空行の場合も keep
            result.append(truncated_line)

    # 中リスク 3: 空出力時は [] を返す（[""] ではなく）
    if not result or (len(result) == 1 and result[0] == ""):
        return [], was_truncated, original_line_count

    return result, was_truncated, original_line_count


def _strip_uv_run_options(argv: List[str]) -> List[str]:
    """
    uv run [options...] <cmd...> を unwrap して <cmd...> の argv を返す。

    uv flag (--locked, --with <pkg> など) を取り除く。
    例: ["uv", "run", "--locked", "pytest"] → ["pytest"]
    例: ["uv", "run", "--with", "pytest", "pytest"] → ["pytest"]
    """
    if not argv or argv[0] != "uv":
        return argv

    if len(argv) < 2 or argv[1] != "run":
        return argv

    # argv[2:] から uv flags を取り除く
    result = []
    i = 2
    while i < len(argv):
        arg = argv[i]
        # uv flags that take an argument
        if arg in ("--with", "--extra", "-p", "--python"):
            i += 2  # skip flag and its argument
            continue
        # uv flags that don't take an argument
        if arg.startswith("--"):
            i += 1  # skip flag only
            continue
        # Non-flag argument: this is the start of the command
        result = argv[i:]
        break

    return result if result else argv


def _is_uv_lock_check(argv: List[str]) -> bool:
    """Return True only for exact `uv lock --check`."""
    return list(argv) == ["uv", "lock", "--check"]


_CANONICAL_RUNTIME_SMOKE_ARGVS: tuple = (
    ["uv", "run", "--isolated", "--locked", "--no-default-groups", "python", "scripts/ci/runtime_dependency_smoke.py"],
    ["uv", "run", "--isolated", "--locked", "--no-default-groups", "python3", "scripts/ci/runtime_dependency_smoke.py"],
)


def _is_uv_runtime_smoke_command(argv: List[str]) -> bool:
    """Return True only for the two canonical runtime smoke command forms."""
    return list(argv) in list(_CANONICAL_RUNTIME_SMOKE_ARGVS)


def _is_pytest_invocation(command: str) -> bool:
    """
    コマンドが pytest invocation かどうかを argv/token ベースで検出。

    対象パターン:
    - pytest
    - python -m pytest
    - python3 -m pytest
    - uv run pytest
    - uv run --locked pytest
    - uv run --with pytest pytest
    - uv run python -m pytest (等々)

    戻り値: True if pytest invocation, False otherwise
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        # shlex.split 失敗 = compound または複雑なコマンド
        return False

    if not argv:
        return False

    # uv run を unwrap
    unwrapped = _strip_uv_run_options(argv)
    if not unwrapped:
        return False

    # unwrapped の最初の要素の basename が pytest か確認
    first_cmd = unwrapped[0]
    if Path(first_cmd).name == "pytest":
        return True

    # python / python3 -m pytest パターン
    if (
        Path(first_cmd).name in ("python", "python3")
        and len(unwrapped) >= 3
        and unwrapped[1] == "-m"
        and unwrapped[2] == "pytest"
    ):
        return True

    return False


_MISSING_NODE_ID_ERROR_RE = re.compile(
    r"error:\s+not found:\s+(?P<nodeid>\S+::\S+)",
    re.IGNORECASE,
)
_NO_MATCH_IN_MODULE_RE = re.compile(
    r"no match in any of"
)


def _is_existing_file_missing_node_id_error(
    stdout: str, stderr: str, cwd: Optional[str] = None
) -> bool:
    """Issue #1347: detect pytest's "existing file, missing node-id" error shape.

    Distinguishes the case where a pytest node-id references a file that DOES
    exist on disk but a test function (or class::function) within it that does
    NOT exist, e.g.:
        ERROR: not found: /path/existing_file.py::test_new_case
        (no match in any of [<Module existing_file.py>])
    from the genuinely-missing-file case, whose pytest error is instead:
        ERROR: file or directory not found: missing_file.py
    Callers must ensure this is only invoked in a context where the FILE part
    is already known to exist (e.g. after _is_regression_gate_command()
    returned True); this helper additionally re-validates that the file part
    of the node-id actually exists on disk (case-insensitively matching the
    "not found" error text and requiring the "no match in any of" companion
    text), so it does not rely solely on the caller's prior check.

    PR #1366 review (Major 3): the error-message match is now case-insensitive
    and requires BOTH the missing-node-id error text AND the "no match in any
    of" companion text (AND, not OR) before re-deriving file existence from
    the node-id's file part, resolved against `cwd`.
    """
    combined = f"{stdout}\n{stderr}"
    match = _MISSING_NODE_ID_ERROR_RE.search(combined)
    if not match:
        return False
    if not _NO_MATCH_IN_MODULE_RE.search(combined):
        return False

    file_part = match.group("nodeid").split("::", 1)[0]
    p = Path(file_part)
    if not p.is_absolute():
        p = Path(cwd or Path.cwd()) / p
    return p.exists()


def _is_regression_gate_command(command: str, cwd: Optional[str] = None) -> bool:
    """
    AC4: regression_gate prefix detection.

    Detects: pnpm typecheck / lint / test / build / uv run pytest <existing-path>

    For pytest commands, requires an existing positional path argument.
    Args:
        command: command string to check
        cwd: working directory (defaults to Path.cwd() if None)

    Returns True if command is a regression gate command, False otherwise.
    """
    if cwd is None:
        cwd = str(Path.cwd())

    # B1: Check for exact pnpm subcommand allowlist (argv-based, not prefix string)
    try:
        argv_check = shlex.split(command)
    except ValueError:
        argv_check = []
    if _canonical_pnpm_gate(argv_check) is not None:
        return True

    # Check for uv run pytest with existing test paths
    if "uv run" not in command or "pytest" not in command:
        return False

    # B3: For pytest commands, extract positional path argument
    try:
        argv = shlex.split(command)
    except ValueError:
        return False

    if not argv:
        return False

    # Strip uv run and options to find positional arguments
    unwrapped = _strip_uv_run_options(argv)
    if not unwrapped:
        return False

    # Find pytest invocation
    pytest_idx = -1
    for i, arg in enumerate(unwrapped):
        if Path(arg).name == "pytest":
            pytest_idx = i
            break
        if (
            Path(arg).name in ("python", "python3")
            and i + 2 < len(unwrapped)
            and unwrapped[i + 1] == "-m"
            and unwrapped[i + 2] == "pytest"
        ):
            pytest_idx = i + 2
            break

    if pytest_idx == -1:
        return False

    # B2: Collect positional arguments after pytest (skip options and their values)
    positional_args = []
    i = pytest_idx + 1
    n = len(unwrapped)
    while i < n:
        arg = unwrapped[i]
        # Options that take a separate value
        if arg in ("-k", "-m", "-p", "-W", "--rootdir", "--basetemp", "--maxfail", "--tb",
                   "--lf-name", "--cache-dir", "-c", "-o"):
            i += 2  # skip flag and its argument
            continue
        # Other flags without arguments
        if arg.startswith("-"):
            i += 1
            continue
        # arg is a positional candidate
        positional_args.append(arg)
        i += 1

    # B2: Check if any positional argument exists and is a valid path
    # Issue #1347: pytest positional args may be node-ids of the form
    # <file>::<test_name> (or <file>::<Class>::<test_name>). The node-id
    # suffix after "::" is never itself a filesystem path, so it must be
    # stripped before the existence check; otherwise a command referencing a
    # NEW test function on an EXISTING file (e.g. existing_file.py::test_new)
    # is incorrectly treated as "path does not exist" even though the file
    # portion is real.
    for arg in positional_args:
        file_part = arg.split("::", 1)[0]
        # Handle both relative and absolute paths
        if Path(file_part).is_absolute():
            test_path = Path(file_part)
        else:
            test_path = Path(cwd) / file_part
        if test_path.exists():
            return True

    # No valid path found
    return False


def _is_negated_search_command(command: str) -> bool:
    """
    AC6: Detect negated search commands like `! rg -q "pattern" file`.

    Returns True if command starts with `!` and contains rg/grep.
    """
    # Check if command starts with ! (possibly with spaces)
    stripped = command.strip()
    if not stripped.startswith("!"):
        return False
    # Check if rg or grep follows after !
    rest = stripped[1:].strip()
    return any(rest.startswith(util) for util in ["rg", "grep"])


def _is_discovery_script(argv: List[str]) -> bool:
    """
    Detect if a command invokes a repository-local ssot-discovery script.

    Discovery scripts are identified by:
    - basename being 'match-ssot.sh' or 'match_ssot.py'
    - OR the script path containing 'ssot-discovery/scripts/'

    Handles invocation forms:
    - bash .claude/skills/ssot-discovery/scripts/match-ssot.sh ...
    - .claude/skills/ssot-discovery/scripts/match-ssot.sh ... (direct)
    - python3 .../match_ssot.py ...
    - uv run python3 .../match_ssot.py ...
    """
    if not argv:
        return False

    # Unwrap 'bash' / 'sh' prefix: argv[0] is shell, look for script in rest
    leading = argv[0]
    script_candidates = []

    cmd_basename = Path(leading).name
    if cmd_basename in ("bash", "sh", "zsh"):
        # Script is first non-option argument after shell invocator
        script_candidates = [a for a in argv[1:] if not a.startswith("-")]
    elif cmd_basename in ("python", "python3"):
        # python3 .../match_ssot.py ... or python3 -m <module>
        # For file-based invocation, script is first non-option positional arg
        script_candidates = [a for a in argv[1:] if not a.startswith("-")]
    elif cmd_basename == "uv":
        # uv run [options] python3 .../match_ssot.py ...
        # Unwrap uv run options
        unwrapped = _strip_uv_run_options(argv)
        if unwrapped and Path(unwrapped[0]).name in ("python", "python3"):
            script_candidates = [a for a in unwrapped[1:] if not a.startswith("-")]
        elif unwrapped:
            script_candidates = [unwrapped[0]]
    else:
        # Direct invocation: check argv[0] itself
        script_candidates = [leading]

    for candidate in script_candidates:
        bname = Path(candidate).name
        if bname in ("match-ssot.sh", "match_ssot.py"):
            return True
        if "ssot-discovery/scripts/" in candidate:
            return True

    return False


def _has_arg(argv: List[str], flag: str) -> bool:
    """
    Check if flag (e.g. '--keywords' or '--paths') appears in argv,
    either as a standalone argument or as the key in '--flag=value' form.
    """
    for arg in argv:
        if arg == flag or arg.startswith(flag + "="):
            return True
    return False


def _rg_has_include_option(argv: List[str]) -> bool:
    """
    AC1 (Issue #648): Detect if an `rg` command uses the `--include` / `--include=...` option.

    `rg` does not have an `--include` option (that is a grep option, not ripgrep).
    When a VC author writes `rg --include=...`, it is likely a mistake (confused with grep).

    Rules:
    - Only applies when argv[0] basename is 'rg'
    - Matches '--include' (standalone) or '--include=...' (value-embedded)
    - Does NOT match '--include-zero' (a different option with different semantics)
    - grep --include is NOT matched (only rg is in scope)

    Returns True if the rg command has an --include option, False otherwise.
    """
    if not argv:
        return False
    if Path(argv[0]).name != "rg":
        return False
    for arg in argv[1:]:
        # Match --include or --include=... but NOT --include-zero
        if arg == "--include":
            return True
        if arg.startswith("--include="):
            return True
        # --include-zero: explicitly excluded
    return False


def _rg_extract_path_operands(argv: List[str]) -> List[str]:
    """
    Extract positional PATH operands from an `rg` argv, correctly skipping
    PATTERN, value-taking flags (`-e`/`--regexp`, `-g`/`--glob`, etc.), and
    honoring `--` as an end-of-options marker.

    This is the mature argv parser originally embedded in
    `_rg_has_broad_search_path()` (Issue #648), extracted so it can be reused
    by other callers (e.g. `_candidate_new_allowed_path_target()`, Issue #1328)
    without duplicating the parsing logic.

    Does NOT validate argv[0] is 'rg'; callers are expected to check that
    themselves (e.g. via `Path(argv[0]).name == "rg"`).

    Returns the list of raw (un-normalized) path operand strings, in the
    order they appear in argv. Returns an empty list if no path operand is
    found (i.e. the command searches the entire repo from cwd).
    """
    if not argv:
        return []

    # Collect positional path arguments (non-option, non-pattern args after pattern)
    # For rg: rg [options] PATTERN [PATH ...]
    # We need to identify the positional args that are paths (after the first positional=PATTERN)
    i = 1
    n = len(argv)
    after_double_dash = False
    first_positional_seen = False
    path_args: List[str] = []

    # Value-taking flags for rg (we need to skip them to find positional args).
    # NOTE: -l / --files-with-matches is a BOOLEAN flag (not value-taking).
    # Including it in value-taking flags would cause the next positional (the pattern)
    # to be consumed as a value, breaking path extraction.
    _RG_VALUE_FLAGS_FOR_PATH = frozenset([
        "-e", "--regexp",
        "-g", "--glob",
        "--iglob",
        "-A", "--after-context",
        "-B", "--before-context",
        "-C", "--context",
        "-m", "--max-count",
        "-M", "--max-columns",
        "--max-depth",
        "--color", "--colours",
        "--type", "-t",
        "--type-not", "-T",
        "--encoding",
        "--field-match-separator",
        "--field-context-separator",
        "--replace", "-r",
        "--pre",
        "--include",   # (invalid for rg but skip gracefully)
        "--exclude",
        "--sortr", "--sort",
        "--threads", "-j",
        "--max-filesize",
        "--context-separator",
        "-f", "--file",  # Issue #1328 Blocker 3: -f/--file PATTERNFILE is value-taking
        # NOTE: -l (--files-with-matches) is boolean, intentionally excluded here
    ])

    # Issue #2900: explicit allow-list of argument-less long boolean flags.
    _RG_LONG_BOOLEAN_FLAGS_FOR_PATH = frozenset([
        "--files-without-match",
        "--fixed-strings",
        "--count",
        "--no-line-number",
    ])

    # Issue #1328 Blocker 3: -f/--file PATTERNFILE supplies the pattern from a
    # file, just like -e/--regexp supplies it inline. In both cases there is
    # no positional PATTERN argument to skip, so all remaining positionals
    # are PATH operands.
    explicit_pattern_given = any(
        arg in ("-e", "--regexp", "-f", "--file")
        or arg.startswith("--regexp=")
        or arg.startswith("--file=")
        or (arg.startswith("-e") and len(arg) > 2)
        or (arg.startswith("-f") and len(arg) > 2 and not arg.startswith("--"))
        for arg in argv[1:]
    )

    while i < n:
        arg = argv[i]

        if arg == "--":
            after_double_dash = True
            i += 1
            continue

        if after_double_dash:
            if not first_positional_seen and not explicit_pattern_given:
                # First arg after -- is PATTERN (skip it)
                first_positional_seen = True
                i += 1
                continue
            else:
                # These are PATHs
                path_args.append(arg)
                i += 1
                continue

        # Handle -e/--regexp: skip value (it's a pattern, not a path)
        if arg in ("-e", "--regexp"):
            explicit_pattern_given = True
            i += 2
            continue
        if arg.startswith("--regexp="):
            explicit_pattern_given = True
            i += 1
            continue
        if arg.startswith("-e") and len(arg) > 2:
            explicit_pattern_given = True
            i += 1
            continue

        # Skip -g/--glob and value
        if arg in ("-g", "--glob", "--iglob"):
            i += 2
            continue
        if arg.startswith("--glob=") or arg.startswith("--iglob="):
            i += 1
            continue

        # Skip other value-taking flags
        if arg in _RG_VALUE_FLAGS_FOR_PATH:
            i += 2
            continue

        # Handle --flag=value forms
        skip_flag_value = False
        for flag in _RG_VALUE_FLAGS_FOR_PATH:
            if flag.startswith("--") and arg.startswith(flag + "="):
                skip_flag_value = True
                break
        if skip_flag_value:
            i += 1
            continue

        # Issue #2900: argument-less long boolean flags (exact match only).
        # These take no value and are neither PATTERN nor PATH. Deliberately NOT
        # a startswith("--") / --no-* generalisation, and `--files` is excluded
        # (separate mode that takes no PATTERN, so positional meaning differs).
        if arg in _RG_LONG_BOOLEAN_FLAGS_FOR_PATH:
            i += 1
            continue

        # Combined short flags (e.g. -nq, -lq): just a flag, no value taken
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            i += 1
            continue

        # Positional argument
        if not first_positional_seen and not explicit_pattern_given:
            # This is the PATTERN (skip)
            first_positional_seen = True
            i += 1
            continue
        else:
            # This is a PATH argument
            path_args.append(arg)
            i += 1
            continue

    return path_args


def _rg_has_broad_search_path(argv: List[str], allowed_paths: Optional[List[str]] = None) -> bool:
    """
    AC2 (Issue #648): Detect if an `rg` command has a broad or unbounded search path.

    Containment-based logic (replaces fixed _BROAD_RG_PATHS list):

    - No positional path argument → broad (ブロック)
    - '.' or '/' path → always broad (ブロック)

    When allowed_paths is given (from Issue body ## Allowed Paths):
    - rg_path == allowed_path (same) → 許可
    - rg_path is a parent of some allowed_path (allowed.startswith(rg_path+"/")) → ブロック (broad)
    - allowed_path is a parent of rg_path (rg_path.startswith(allowed+"/")) → 許可 (narrowed)
    - rg_path not covered by any allowed_path at all → ブロック

    When allowed_paths is None or empty (fallback):
    - Only '.' and '/' are blocked (conservative behavior preserved).

    This function only inspects rg commands (argv[0] basename 'rg').
    grep / egrep / fgrep are NOT in scope.

    Returns True if the rg command has a broad/unbounded search path.
    """
    if not argv:
        return False
    if Path(argv[0]).name != "rg":
        return False

    # Path operand extraction is delegated to _rg_extract_path_operands()
    # (Issue #1328: shared argv parser, no duplicated parsing logic).
    path_args = _rg_extract_path_operands(argv)

    # No path arguments: searches the entire repo → broad
    if not path_args:
        return True

    # Normalize: strip trailing slash for comparison
    def _normalize_path(p: str) -> str:
        return p.rstrip("/")

    # Build allowed path set (normalized) from allowed_paths
    allowed_normalized: List[str] = []
    if allowed_paths:
        for ap in allowed_paths:
            allowed_normalized.append(_normalize_path(ap))

    for path_arg in path_args:
        normalized = _normalize_path(path_arg)

        # '.' or '/' always broad regardless of allowed_paths
        if normalized in (".", "", "/"):
            return True

        if not allowed_normalized:
            # Fallback (no Allowed Paths in Issue): only '.' and '/' are blocked.
            # Any explicit path is conservatively allowed.
            continue

        # Containment check using allowed_paths:
        # - rg_path == allowed_path → allowed (same target)
        # - allowed_path starts with rg_path+"/" → rg_path is a PARENT of allowed → broad (blocked)
        # - rg_path starts with allowed_path+"/" → rg_path is UNDER allowed → allowed (narrowed)
        is_covered = False
        is_parent_of_allowed = False
        for ap in allowed_normalized:
            if normalized == ap:
                is_covered = True
                break
            if normalized != "" and ap.startswith(normalized + "/"):
                # rg_path is a parent directory of some allowed_path → broad
                is_parent_of_allowed = True
            if normalized.startswith(ap + "/"):
                # rg_path is a sub-path of allowed_path → narrowed, covered
                is_covered = True
                break

        if is_parent_of_allowed and not is_covered:
            return True
        if not is_covered and not is_parent_of_allowed:
            # Path not covered by any allowed_path at all → broad
            return True

    return False


def _is_trivially_pass_command(command: str) -> bool:
    """
    Detect trivially-pass VC patterns: repository-local ssot-discovery scripts
    called with BOTH --keywords and --paths options simultaneously.

    Root cause (Issue #201):
      match-ssot.sh / match_ssot.py implements a directory mapping that forces
      --paths targets into matched_documents at low relevance, regardless of
      whether --keywords actually appear in that path. When both --keywords and
      --paths are supplied, the VC can return the target path (exit 0) even
      when the keywords are absent — making the VC trivially-passing.

    Detection logic:
    - The command invokes a discovery script (match-ssot.sh / match_ssot.py /
      any script under ssot-discovery/scripts/)
    - AND the argument list contains BOTH --keywords (or --keywords=...) AND
      --paths (or --paths=...)

    Only the combination of --keywords + --paths creates the trivially-pass
    structure. --keywords alone or --paths alone does not trigger this.

    Note: rg/grep have no '--paths' option. rg uses positional PATH arguments
    and -g/--glob for path filtering. Positional paths in rg/grep are not
    forced-includes and are NOT trivially-pass. The previous implementation
    that flagged rg/grep + '--paths' was based on an incorrect premise and
    caused false positives for queries like 'rg -e "--paths" file' or
    'rg -- "--paths" file'. That logic has been removed.

    Returns True if the command is a trivially-pass pattern, False otherwise.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False

    if not argv:
        return False

    # Step 1: Identify whether this is a discovery script invocation
    if not _is_discovery_script(argv):
        return False

    # Step 2: Check for --keywords AND --paths both present
    # Only the combination creates the forced-include trivially-pass structure.
    has_keywords = _has_arg(argv, "--keywords")
    has_paths = _has_arg(argv, "--paths")
    return has_keywords and has_paths


def has_command_substitution(command: str) -> bool:
    """
    B3: Detect command substitution patterns: $(...), `...`, ${...}
    ONLY in unquoted segments.

    Single-quoted segments are excluded — they are literals to subprocess.
    """
    # Scan the original command character by character, tracking quote state.
    # Single-quoted content is literal, double-quoted content may have substitution.
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        # Only check for substitution outside of single quotes
        if not in_single:
            # $(...) or ${...} or backtick
            if ch == '$' and i + 1 < len(command) and command[i + 1] in ('(', '{'):
                return True
            if ch == '`':
                return True
        i += 1
    return False


# ---------------------------------------------------------------------------
# Static command classification: allowlist / denylist policy (AC1-AC3)
# ---------------------------------------------------------------------------

# Commands that are explicitly denied (unsafe for baseline preflight)
# Checked by the basename of argv[0]
_DENIED_COMMANDS: frozenset = frozenset([
    # Shell invocations
    "bash", "sh", "zsh", "fish", "dash", "ksh",
    # Inline code execution (python -c / python3 -c / node -e / perl -e / ruby -e)
    # NOTE: python3 is special-cased below: only -m py_compile / -m pytest is allowed
    "python",
    "node", "perl", "ruby",
    # Network access
    "curl", "wget", "nc", "ncat", "ssh", "scp", "rsync",
    # Filesystem mutation
    "rm", "mv", "cp", "chmod", "chown", "touch",
    "rmdir", "ln",
    # Text stream mutation (sed -i is a mutation; all sed uses blocked for safety)
    "sed", "tee",
])

# B3: git read-only subcommand allowlist (exact argv[1] check)
# git -c / --config-env / --exec-path / alias.* flags → blocked via option-flag check
# git worktree, git submodule, git bisect, etc. (not listed here) → blocked by default
_ALLOWED_GIT_SUBCOMMANDS: frozenset = frozenset([
    "status", "diff", "log", "show", "ls-files", "rev-parse", "branch", "tag", "-l",
])

# B3: gh read-only subcommand allowlist (exact argv tuple prefix check)
# gh alias, gh extension, gh auth (mutation), etc. → blocked by default
_ALLOWED_GH_PREFIXES: tuple = (
    # Read-only tuples: (argv[1],) or (argv[1], argv[2])
    ("issue", "view"),
    ("pr", "view"),
    ("pr", "list"),
    ("issue", "list"),
    ("repo", "view"),
    # NOTE: gh api is blocked — mutation potential via POST/PATCH; not needed in VC context
)

# B1: pnpm exact subcommand allowlist (tuple-based)
# Only these exact (argv[0], argv[1]) tuples are allowed for pnpm.
# pnpm exec, pnpm dlx, pnpm run, pnpm add, etc. are blocked.
# Compatibility names are derived from the shared registry; they are not
# independent allowlists.
_ALLOWED_PNPM_SUBCOMMANDS: frozenset = frozenset(
    gate.request_argv for gate in pnpm_gate_registry.iter_gate_descriptors()
)
_FIXED_ENV_DELTA_BY_COMMAND: Dict[Tuple[str, str], Dict[str, str]] = {
    gate.request_argv: dict(pnpm_gate_registry.RUNNER_ENV_DELTA)
    for gate in pnpm_gate_registry.iter_gate_descriptors()
}

_PNPM_NO_TTY_ERROR_PATTERNS: tuple[str, ...] = (
    "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY",
    "Aborted removal of modules directory due to no TTY",
)

# Explicitly allowed command basenames for baseline preflight
# Anything NOT in this set is blocked by default (allowlist-closed policy, AC3)
_ALLOWED_COMMANDS: frozenset = frozenset([
    "test",      # test -f / -d / -s (read-only assertions)
    "rg",        # ripgrep (read-only)
    "grep",      # grep (read-only)
    "fgrep",
    "egrep",
    "python3",   # allowed only when _is_allowed_python3_invocation passes
    "uv",        # allowed only for uv run pytest / uv run python3 -m pytest
    "pnpm",      # allowed only for typecheck/lint/test/build subcommands
    "pytest",    # direct pytest invocation
    "git",       # allowed only for read-only subcommands (show, log, diff, etc.)
    "gh",        # allowed only for read-only subcommands (gh issue view, gh pr view)
    "jq",        # read-only JSON filter
    "cat",       # read-only
    "ls",        # read-only
    "find",      # read-only
    "wc",        # read-only
    "sort",      # read-only
    "uniq",      # read-only
    "head",      # read-only
    "tail",      # read-only
    "diff",      # read-only
    "echo",      # safe
    "printf",    # safe
    "true",      # safe
    "false",     # safe
    "realpath",  # safe
    "dirname",   # safe
    "basename",  # safe
    "which",     # safe
    "type",      # safe
    "env",       # display-only; command wrapper/env injection denied by _is_allowed_env_invocation()
    "printenv",  # safe (read env)
    "pwd",       # safe
    "date",      # safe
    "stat",      # read-only
    # NOTE: mkdir removed from allowlist (B2) — mkdir -p .git/hooks and similar mutations possible
])


def _canonical_pnpm_gate(argv: List[str]) -> Optional[Tuple[str, str]]:
    """Return the canonical 2-token pnpm gate tuple, or None if not exact."""
    descriptor = pnpm_gate_registry.gate_for_request(argv)
    return descriptor.request_argv if descriptor else None


def _is_allowed_python3_invocation(argv: List[str]) -> bool:
    """
    python3 is only allowed for:
      - python3 -m py_compile <file>
      - python3 -m pytest ...
    Inline code (-c flag) is NOT allowed (AC2: python3 -c is blocked as unsafe_command).
    """
    if not argv or Path(argv[0]).name not in ("python3",):
        return False
    if len(argv) >= 2 and argv[1] == "-c":
        return False  # inline code not allowed
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] in ("py_compile", "pytest"):
        return True
    return False


def _is_allowed_git_invocation(argv: List[str]) -> bool:
    """
    B3: git read-only allowlist check.
    Returns True only if the git subcommand is in the read-only allowlist.
    Blocks git -c, --config-env, --exec-path option flags, and any unlisted subcommand.
    """
    if not argv or Path(argv[0]).name != "git":
        return False
    if len(argv) < 2:
        return False
    # Block global git option flags (e.g., git -c alias.x='!...')
    second_arg = argv[1]
    if second_arg.startswith("-") and second_arg not in ("-l",):
        return False
    subcommand = second_arg.lower()
    return subcommand in _ALLOWED_GIT_SUBCOMMANDS


def _is_allowed_gh_invocation(argv: List[str]) -> bool:
    """
    B3: gh read-only allowlist check.
    Returns True only if the gh subcommand tuple is in the read-only allowlist.
    Blocks gh alias, gh extension, gh auth (mutations), gh api, and any unlisted subcommand.
    """
    if not argv or Path(argv[0]).name != "gh":
        return False
    if len(argv) < 3:
        return False
    # Check (argv[1], argv[2]) tuple
    sub_tuple = (argv[1].lower(), argv[2].lower())
    return sub_tuple in _ALLOWED_GH_PREFIXES


def _is_allowed_pnpm_invocation(argv: List[str]) -> bool:
    """
    B1: pnpm exact subcommand allowlist check.
    Only (pnpm, typecheck), (pnpm, lint), (pnpm, test), (pnpm, build) are allowed.
    pnpm exec, pnpm dlx, pnpm run, pnpm add, etc. are all blocked.
    """
    return _canonical_pnpm_gate(argv) is not None


def _is_allowed_env_invocation(argv: List[str]) -> bool:
    """Allow `env` only for read-only display, never as a command wrapper."""
    if not argv or Path(argv[0]).name != "env":
        return False
    if len(argv) == 1:
        return True
    return len(argv) == 2 and argv[1] in ("--help", "--version")


# Issue #2638 P2 fix (PR #2643 review Finding 3): sentinel value for an
# `env_delta` dict entry meaning "unset this environment variable for the
# VC subprocess" -- as opposed to a normal string value, which means "set
# this environment variable to this value". `_fixed_env_delta_for_argv()`'s
# return type is `Dict[str, str]` (a flat key -> value delta) and cannot
# natively express key deletion; this sentinel closes that gap without
# widening the function's contract. `run_command()` below is the single
# place that interprets this sentinel when applying `env_delta` to the
# subprocess's environment.
_ENV_UNSET_SENTINEL = "__baseline_vc_preflight_env_unset__"


def _fixed_env_delta_for_argv(argv: List[str]) -> Dict[str, str]:
    """Return a fixed runner-side env delta for exact safe commands only.

    Issue #2638 P2 fix (PR #2643 review Finding 3): two independent,
    additive conditions may each contribute keys to the returned delta --
    the pre-existing pnpm-gate delta (keyed off `_canonical_pnpm_gate(argv)`)
    and rg's `RIPGREP_CONFIG_PATH` isolation delta (keyed off
    `os.path.basename(argv[0]) == "rg"`). Neither branch overwrites the
    other; both are evaluated and merged into the same dict (in practice a
    single argv cannot match both conditions, since pnpm and rg are
    different executables)."""
    delta: Dict[str, str] = {}
    key = _canonical_pnpm_gate(argv)
    if key is not None:
        delta.update(pnpm_gate_registry.RUNNER_ENV_DELTA)
    if argv and os.path.basename(argv[0]) == "rg":
        # A user's shell/global environment may set `RIPGREP_CONFIG_PATH`
        # to point at a config file containing an option that is invalid
        # or unsupported by the invoked rg binary. ripgrep prepends the
        # config file's own arguments to the CLI argv and reparses them
        # with the SAME parser (verified locally against ripgrep 14.1.0:
        # a config file containing `--definitely-invalid-option` makes
        # even the well-formed VC `rg -F foo sample.txt` fail with
        # `rg: unrecognized flag --definitely-invalid-option`, exit code
        # 2 -- identical in shape to a genuinely broken VC command line).
        # Without this isolation, `_detect_rg_grep_exit2_syntax_error()`
        # would misclassify this pre-existing environment/config condition
        # as a body-author-fixable `vc_grep_syntax_error`, even though the
        # VC command text itself is correct. Isolate ONLY this VC
        # subprocess (never the caller's actual shell/global environment --
        # `run_command()` applies `env_delta` to a COPY of `os.environ`,
        # never to `os.environ` itself) by unsetting `RIPGREP_CONFIG_PATH`
        # via the sentinel above. Scoped to `prog == "rg"` only: grep and
        # every other command are unaffected.
        delta["RIPGREP_CONFIG_PATH"] = _ENV_UNSET_SENTINEL
    return delta


def _is_package_manager_no_tty_prompt(command: str, stdout: str, stderr: str) -> bool:
    """Detect package-manager no-TTY prompts that are tooling/env blockers."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False

    if _canonical_pnpm_gate(argv) is None:
        return False

    combined = f"{stdout}\n{stderr}"
    return any(pattern in combined for pattern in _PNPM_NO_TTY_ERROR_PATTERNS)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# GitHub metadata assertion — Issue #942
# ---------------------------------------------------------------------------

# Allowed milestone metadata fields for github_metadata_assert (Issue #942 review BLOCKER 1).
# A field outside this allowlist (e.g. a 'description' typo like 'descripton') is rejected at
# classify time rather than silently treated as an absent field, which would let not_contains
# false-pass.
_ALLOWED_GITHUB_METADATA_FIELDS = {"description"}


def _is_github_metadata_assert_command(command: str) -> bool:
    """
    Detect if a command is a github_metadata_assert assertion.

    Format: github_metadata_assert <contains|not_contains> <field> <literal> <endpoint> [flags...]

    Returns True if the command starts with 'github_metadata_assert'.
    """
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False

    if not argv:
        return False

    return Path(argv[0]).name == "github_metadata_assert"


def _is_allowed_github_metadata_assert(argv: List[str]) -> Optional[Tuple[bool, Optional[str]]]:
    """
    Validate a github_metadata_assert command (Issue #942).

    Format (exactly 4 arguments after the command name; NO flags, NO extra positional):
        github_metadata_assert <contains|not_contains> <field> <literal> <endpoint>

    Returns: (is_valid, error_message)
      - (True, None): command is valid
      - (False, error_msg): command is invalid with explanation

    Validation rules:
    - Exactly 5 argv tokens (command + assertion_type + field + literal + endpoint).
      Any extra token is rejected. Because no flag surface is accepted at all, every
      dangerous flag (-f/-F/--field/--raw-field/--input/--header/-H/--include/-i/
      --paginate/--slurp/--cache/--template/--preview/graphql, in any case variant) and
      every mutating method (--method POST/PATCH/PUT/DELETE, -X ..., --method=...) is
      rejected here by construction (review BLOCKER 2 / MAJOR 1).
    - assertion_type must be contains or not_contains.
    - field must be in _ALLOWED_GITHUB_METADATA_FIELDS; a typo or unknown field is rejected
      rather than silently treated as an absent field (review BLOCKER 1 / MAJOR 2).
    - endpoint must match repos/<owner>/<repo>/milestones/<number>; absolute URLs, query
      strings, path traversal and placeholders are rejected (AC2).
    """
    if len(argv) != 5:
        return False, (
            "github_metadata_assert accepts exactly 4 arguments "
            "(assertion_type field literal endpoint) and no flags; got "
            f"{max(len(argv) - 1, 0)}"
        )

    cmd_name = Path(argv[0]).name
    if cmd_name != "github_metadata_assert":
        return False, "Not a github_metadata_assert command"

    assertion_type = argv[1].lower()
    if assertion_type not in ("contains", "not_contains"):
        return False, f"assertion_type must be 'contains' or 'not_contains', got '{argv[1]}'"

    field = argv[2]
    if field not in _ALLOWED_GITHUB_METADATA_FIELDS:
        return False, (
            f"field '{field}' is not allowed; allowed fields: "
            f"{sorted(_ALLOWED_GITHUB_METADATA_FIELDS)}"
        )

    literal = argv[3]
    if not literal:
        return False, "literal argument is missing"

    endpoint = argv[4]

    # Validate endpoint (AC2)
    if endpoint.startswith('http://') or endpoint.startswith('https://') or endpoint.startswith('//'):
        return False, "endpoint must not be an absolute URL; use relative path like 'repos/owner/repo/milestones/1'"

    if '?' in endpoint:
        return False, "endpoint must not contain query strings (?)"

    if '..' in endpoint:
        return False, "endpoint must not contain path traversal (..)"

    if '<' in endpoint or '>' in endpoint:
        return (
            False,
            "endpoint must not contain placeholders (<...>); use actual values like 'repos/owner/repo/milestones/1'"
        )

    if not re.match(r'^repos/[^/]+/[^/]+/milestones/\d+$', endpoint):
        return False, f"endpoint must match 'repos/<owner>/<repo>/milestones/<number>', got '{endpoint}'"

    return True, None


def _check_github_metadata_assertion(
    assertion_type: str,
    field: str,
    literal: str,
    endpoint: str,
    timeout_seconds: int = 10,
) -> int:
    """
    Execute GitHub metadata assertion via gh api.

    Internal implementation for github_metadata_assert.
    Uses fixed argv to avoid shell injection: ["gh", "api", "--method", "GET", f"repos/..."]

    Args:
        assertion_type: "contains" or "not_contains"
        field: field name to check (e.g., "description")
        literal: literal string to search for
        endpoint: full endpoint path (e.g., "repos/owner/repo/milestones/1")
        timeout_seconds: timeout for gh command

    Returns:
        Exit code:
          - 0: assertion passed
          - 1: assertion failed (but gh API succeeded)
          - 2: gh command not found
          - 3: gh authentication failed
          - 4: 404 not found (resource doesn't exist)
          - 5: rate limited (429)
          - 6: timeout
          - 7: invalid JSON response
          - 8: other / unknown gh failure (network, 5xx, secondary rate limit, ...)
          - 9: requested field absent from API response (schema error)

    Raises:
        subprocess.TimeoutExpired: if gh command times out
        json.JSONDecodeError: if gh response is not valid JSON
    """
    argv = ["gh", "api", "--method", "GET", endpoint]

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except FileNotFoundError:
        # gh command not found
        return 2
    except subprocess.TimeoutExpired:
        return 6

    # Check for HTTP errors via stderr/exit code
    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if "not authenticated" in stderr_lower or "authentication failed" in stderr_lower or "401" in result.stderr:
            return 3
        if "404" in result.stderr or "not found" in stderr_lower:
            return 4
        if "429" in result.stderr or "rate limit" in stderr_lower:
            return 5
        # Any other nonzero gh exit is an environment / transport / API failure
        # (network error, 5xx, 403 secondary rate limit, gh version drift, ...),
        # NOT a semantic assertion failure. Never collapse it to exit 1 (review BLOCKER 3).
        return 8

    # Parse JSON response
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return 7

    # The requested field must be present in the API response. A missing field
    # (schema drift / renamed field) is NOT 'literal absent': collapsing it to an empty
    # string would let not_contains false-pass (review BLOCKER 1). Treat it as a
    # schema/environment error distinct from assertion pass/fail.
    if field not in data:
        return 9
    raw_value = data.get(field)
    field_value = "" if raw_value is None else str(raw_value)

    # Perform assertion
    is_present = literal in field_value

    # Return exit code based on assertion type
    if assertion_type == "contains":
        # contains: present → 0, absent → 1
        return 0 if is_present else 1
    else:  # not_contains
        # not_contains: present → 1, absent → 0
        return 1 if is_present else 0


# Regex-bearing command detection for backslash-pipe (regex_literal_pipe_suspected) — Issue #589
# ---------------------------------------------------------------------------


def _is_regex_bearing_command_for_literal_pipe(argv: List[str]) -> bool:
    """
    Return True if the command is regex-bearing and uses a regex engine where
    backslash-pipe is a literal pipe (not alternation).

    Coverage:
    - rg: Rust regex engine, x|y is alternation, backslash-pipe is literal pipe.
      EXCEPT: rg -F / rg --fixed-strings disables regex entirely → return False.
    - egrep: ERE, backslash-pipe is literal pipe character (not alternation) → True.
    - fgrep: fixed-string grep (no regex engine) → always False (Blocker 2 fix).
    - grep -E / grep -P: Extended/Perl regex → True.
      EXCEPT: grep -F / grep --fixed-strings disables regex → return False (Blocker 2 fix).

    Note: grep (basic mode, BRE) also treats \\| as literal, but since BRE
    uses | as literal anyway (alternation needs \\|), this check focuses on
    the cases where \\| is clearly wrong intent (author likely intended |
    as alternation).
    """
    if not argv:
        return False
    cmd_basename = Path(argv[0]).name

    if cmd_basename == "rg":
        # rg -F / --fixed-strings: not a regex-bearing command
        for arg in argv[1:]:
            if arg in ("-F", "--fixed-strings"):
                return False
            # Combined short flags like -Fn, -nF etc.
            if arg.startswith("-") and not arg.startswith("--") and "F" in arg[1:]:
                return False
        return True

    if cmd_basename == "egrep":
        # egrep is always ERE → True
        return True

    if cmd_basename == "fgrep":
        # fgrep is fixed-string grep → not regex-bearing (Blocker 2 fix)
        return False

    if cmd_basename == "grep":
        has_fixed_strings = False
        has_extended_or_perl = False
        for arg in argv[1:]:
            if arg in ("-F", "--fixed-strings"):
                has_fixed_strings = True
            elif arg in ("-E", "-P", "--extended-regexp", "--perl-regexp"):
                has_extended_or_perl = True
            elif arg.startswith("-") and not arg.startswith("--"):
                flags = arg[1:]
                if "F" in flags:
                    has_fixed_strings = True
                if "E" in flags or "P" in flags:
                    has_extended_or_perl = True
        # -F takes precedence: fixed-string mode, not regex-bearing
        if has_fixed_strings:
            return False
        return has_extended_or_perl

    return False


def _command_pattern_contains_backslash_pipe(argv: List[str]) -> bool:
    r"""
    Check if the PATTERN argument (only) in argv contains \\|.

    This detects the case where a user wrote \\| intending regex literal pipe,
    which in most regex engines (rg, egrep, grep -E) is NOT needed — | alone
    is alternation in ERE/Rust regex, and \\| is a literal pipe.

    Blocker 3 fix: Only the PATTERN argument is inspected, not PATH/GLOB/option values.

    For rg:
      - -e PATTERN / --regexp PATTERN → the value is a pattern
      - -g GLOB / --glob GLOB → not inspected (glob, not regex pattern)
      - -F / --fixed-strings → caller already returns False from _is_regex_bearing_command
      - -- PATTERN PATH... → first positional after -- is PATTERN; the rest are PATHs
      - Without -e/--regexp: first non-flag positional argument is PATTERN; rest are PATHs

    For grep / egrep:
      - -e PATTERN / --regexp PATTERN → the value is a pattern
      - Without -e/--regexp: first non-flag positional argument is PATTERN; rest are PATHs
      - File PATH arguments are NOT inspected.
    """
    if not argv:
        return False

    cmd_basename = Path(argv[0]).name
    n = len(argv)

    # Flags that take a value argument for both rg and grep families.
    # We need to skip these flag+value pairs to correctly identify positional args.
    # Value-taking flags common to rg/grep (non-exhaustive; focus on pattern-relevant ones):
    _RG_VALUE_FLAGS = frozenset([
        "-e", "--regexp",          # pattern (handled specially below)
        "-g", "--glob",            # glob (NOT a pattern)
        "-A", "--after-context",
        "-B", "--before-context",
        "-C", "--context",
        "-m", "--max-count",
        "-M", "--max-columns",
        "--max-depth",
        # NOTE: -l / --files-with-matches is a BOOLEAN flag, NOT value-taking.
        # Do NOT include -l here — it would cause the pattern positional arg to be
        # consumed as the value of -l, breaking pattern detection.
        "--color", "--colours",
        "--type", "-t",
        "--type-not", "-T",
        "--encoding", "-E",        # Note: -E in rg is --encoding, not --extended-regexp
        "--field-match-separator",
        "--field-context-separator",
        "--replace", "-r",
        "--pre",
        "--iglob",
    ])

    _GREP_VALUE_FLAGS = frozenset([
        "-e", "--regexp",          # pattern (handled specially below)
        "-f", "--file",
        "-A", "--after-context",
        "-B", "--before-context",
        "-C", "--context",
        "-m", "--max-count",
        "--label",
        "--color", "--colour",
        "--binary-files",
        "-D", "--devices",
        "-d", "--directories",
        "--include",
        "--exclude",
        "--exclude-from",
        "--exclude-dir",
    ])

    # Collect explicit pattern arguments from -e/--regexp
    explicit_patterns: List[str] = []

    if cmd_basename == "rg":
        value_flags = _RG_VALUE_FLAGS
    else:
        # grep, egrep, fgrep
        value_flags = _GREP_VALUE_FLAGS

    i = 1
    after_double_dash = False
    first_positional_seen = False

    while i < n:
        arg = argv[i]

        if arg == "--":
            after_double_dash = True
            i += 1
            continue

        if after_double_dash:
            # After --: first arg is PATTERN (if no -e was given), rest are PATHs
            if not first_positional_seen and not explicit_patterns:
                # This is the PATTERN
                if "\\|" in arg:
                    return True
                first_positional_seen = True
            # Remaining args after -- are PATHs: do NOT inspect
            i += 1
            continue

        # Handle -e PATTERN / --regexp PATTERN (explicit pattern flag)
        if arg in ("-e", "--regexp"):
            if i + 1 < n:
                explicit_patterns.append(argv[i + 1])
            i += 2
            continue

        # Handle --regexp=PATTERN form
        if arg.startswith("--regexp="):
            explicit_patterns.append(arg[len("--regexp="):])
            i += 1
            continue

        # Handle -eXXX (short flag with value concatenated, e.g. -e"foo\|bar")
        if arg.startswith("-e") and len(arg) > 2:
            explicit_patterns.append(arg[2:])
            i += 1
            continue

        # Skip -g / --glob and their values (NOT pattern) for rg
        if cmd_basename == "rg" and arg in ("-g", "--glob", "--iglob"):
            i += 2  # skip flag and value
            continue
        if cmd_basename == "rg" and (arg.startswith("--glob=") or arg.startswith("--iglob=")):
            i += 1  # skip flag=value
            continue

        # Skip other known value-taking flags and their values
        if arg in value_flags:
            i += 2  # skip flag and value
            continue

        # Handle --flag=value forms for value-taking flags
        for flag in value_flags:
            if flag.startswith("--") and arg.startswith(flag + "="):
                i += 1
                arg = None  # consumed
                break
        if arg is None:
            continue

        # Combined short flags (e.g. -nq, -rn): skip (no value taken beyond the flag itself)
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            i += 1
            continue

        # Positional argument
        if not first_positional_seen and not explicit_patterns:
            # This is the PATTERN (only if no -e was given)
            if "\\|" in arg:
                return True
            first_positional_seen = True
        # else: it's a PATH → do NOT inspect
        i += 1

    # Check any explicitly collected patterns
    for pat in explicit_patterns:
        if "\\|" in pat:
            return True

    return False


def has_unquoted_inline_baseline_expect(command: str) -> bool:
    """AC2/AC12: detect an inline '# baseline-expect:' annotation embedded in a
    command string OUTSIDE any quoted region. Quoted literals such as
    `rg "# baseline-expect: pass" file` must NOT match."""
    in_single = False
    in_double = False
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "#" and not in_single and not in_double:
            rest = command[i + 1:].lstrip()
            if rest.startswith("baseline-expect:"):
                return True
        i += 1
    return False


def _candidate_new_allowed_path_target(
    command: str, allowed_paths: Optional[List[str]], cwd: Optional[str]
) -> Optional[str]:
    """AC4/AC14: if the command is `test -f|-e|-s PATH` or `rg ... PATH` whose PATH
    is within Allowed Paths and does NOT exist at the baseline cwd, return that
    PATH; otherwise None. Conservative shlex-based extraction; callers gate on
    classify_static_command() returning None first (so unsafe/broad are excluded)."""
    if not allowed_paths:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    norm_allowed = [p.strip().lstrip("./").rstrip("/") for p in allowed_paths if p.strip()]

    def _in_allowed(p: str) -> bool:
        pp = p.lstrip("./")
        return any(pp == a or pp.startswith(a + "/") for a in norm_allowed)

    candidates: List[str] = []
    if prog == "test":
        for a in argv[1:]:
            if a.startswith("-"):
                continue
            candidates.append(a)
    elif prog == "rg":
        # Issue #1328 (AC7): use the mature argv parser shared with
        # _rg_has_broad_search_path() instead of a naive non_opt[1:] split,
        # so -e/--regexp/--glob/--/value-taking flags are handled correctly.
        candidates.extend(_rg_extract_path_operands(argv))
    else:
        return None

    base = cwd or "."
    for c in candidates:
        if _in_allowed(c) and not os.path.exists(os.path.join(base, c)):
            return c
    return None


def classify_static_command(
    raw_command: str, cwd: Path, allowed_paths: Optional[List[str]] = None
) -> Optional[Tuple[str, str, str, Optional[str], str]]:
    """
    Perform static pre-execution classification of a VC command.

    Returns (classification, category, decision, fix_hint, scope_class) if
    the command is blocked or can be determined statically without execution,
    or None if the command should proceed to run_command().

    This is called BEFORE run_command() to prevent dangerous commands from
    being executed. AC1-AC3 enforcement happens here.

    Args:
        raw_command: the raw command string from the VC block
        cwd: working directory
        allowed_paths: list of Allowed Paths from Issue body ## Allowed Paths section.
            Used by _rg_has_broad_search_path for containment-based broad path detection.
            If None or empty, falls back to conservative behavior (block '.' and '/' only).
    """
    # 1. Check for unsupported shell syntax: $(...), `...`, ${...}  (AC1)
    if has_command_substitution(raw_command):
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            "Shell substitution ($(...), `...`, ${...}) is not supported in VC preflight; "
            "use a direct command without command substitution",
            "baseline_fail_expected",
        )

    # 2. Try to parse with shlex (AC1 edge: malformed shell syntax)
    try:
        argv = shlex.split(raw_command, posix=True)
    except ValueError as e:
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            f"Cannot parse command with shlex: {e}; check for unmatched quotes or unsupported syntax",
            "baseline_fail_expected",
        )

    if not argv:
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            "Empty command after parsing",
            "baseline_fail_expected",
        )

    cmd_basename = Path(argv[0]).name

    # 3. Check for compound commands (shell operators)
    if detect_compound_command(raw_command):
        return (
            "blocked",
            "compound_command_disallowed",
            "blocked",
            "Compound shell commands are not supported in baseline_vc_preflight/v1",
            "baseline_fail_expected",
        )

    # 3.5. Trivially-pass detection: discovery script with --keywords + --paths.
    # NOTE: This check runs BEFORE denied-command detection (step 4) so that
    # 'bash match-ssot.sh --keywords ... --paths ...' is reported as
    # category: trivially_pass rather than category: unsafe_command.
    # Both results are classification: blocked / decision: blocked / exit_code: None
    # (neither is executed), so moving this check earlier is a category-label
    # correction, not a safety relaxation.
    if _is_trivially_pass_command(raw_command):
        return (
            "blocked",
            "trivially_pass",
            "blocked",
            "Discovery script (match-ssot.sh / match_ssot.py) is called with both "
            "--keywords and --paths; the directory mapping in match-ssot forces --paths "
            "targets into matched_documents regardless of keyword presence, making the VC "
            "trivially pass. Use --keywords only (without --paths) and verify the same path "
            "appears in results to confirm keyword presence.",
            "baseline_fail_expected",
        )

    # 3.6. Regex literal pipe detection: rg/egrep/grep -E with \\| in pattern (AC3: Issue #589)
    # \\| in a regex-bearing command pattern is likely a mistake (intending literal pipe
    # while the engine treats | as alternation and \\| as literal pipe).
    # This is classified as regex_literal_pipe_suspected and blocked unless the caller
    # supplies a literal-pipe-ok annotation (handled at parse/caller level).
    # NOTE: This check runs BEFORE broad_search_path_unbounded (3.6b) to preserve #589 behavior:
    # a command with both \\| in pattern AND a broad path is reported as regex_literal_pipe_suspected.
    if _is_regex_bearing_command_for_literal_pipe(argv):
        if _command_pattern_contains_backslash_pipe(argv):
            return (
                "blocked",
                "regex_literal_pipe_suspected",
                "blocked",
                "Pattern argument contains \\| in a regex-bearing command (rg/egrep/grep -E). "
                "In ripgrep and ERE-mode grep, | is alternation and \\| is a literal pipe. "
                "If you intend regex alternation, use | (without backslash). "
                "If you truly need a literal pipe in the pattern, add "
                "# vc-regex-intent: literal-pipe-ok reason=\"...\" on the preceding line.",
                "baseline_fail_expected",
            )

    # 3.6a. rg --include option mismatch detection (AC1: Issue #648)
    # rg does not have --include (that is a grep option). Using --include with rg is a VC mistake.
    # --include-zero is a valid rg option and is excluded from detection.
    # grep --include is valid (grep syntax) and is excluded.
    if _rg_has_include_option(argv):
        return (
            "blocked",
            "rg_option_mismatch",
            "blocked",
            "rg does not have --include / --include=... (that is a grep option). "
            "Use -g / --glob for file filtering in ripgrep (e.g., rg -g '*.py' pattern). "
            "If you intended grep, use grep --include instead.",
            "baseline_fail_expected",
        )

    # 3.6b. rg broad search path detection (AC2: Issue #648)
    # rg without path or with repo-root/broad-dir path searches the entire repo,
    # making the VC pass on unrelated existing assets.
    if _rg_has_broad_search_path(argv, allowed_paths=allowed_paths):
        return (
            "blocked",
            "broad_search_path_unbounded",
            "blocked",
            "rg search path is too broad (no path / repo root / broad directory like docs/ or src/). "
            "Narrow the search path to the Allowed Paths or a specific file/directory "
            "(e.g., rg 'pattern' .claude/skills/... or rg 'pattern' docs/product/specific-file.md). "
            "Broad searches may produce false positives from existing assets unrelated to this Issue.",
            "baseline_fail_expected",
        )

    # 4. Check denied commands (unsafe, AC2)
    if cmd_basename in _DENIED_COMMANDS:
        return (
            "blocked",
            "unsafe_command",
            "blocked",
            f"'{cmd_basename}' is not safe for baseline preflight; "
            "shell interpreters, network tools, and filesystem mutators are blocked",
            "baseline_fail_expected",
        )

    # 5. Special case: python3 with -c flag (AC2: python3 -c is blocked)
    if cmd_basename == "python3" and not _is_allowed_python3_invocation(argv):
        # python3 without a recognized safe invocation pattern
        if len(argv) >= 2 and argv[1] == "-c":
            return (
                "blocked",
                "unsafe_command",
                "blocked",
                "python3 -c (inline code) is not allowed in VC preflight; "
                "use python3 -m pytest or python3 -m py_compile instead",
                "baseline_fail_expected",
            )
        # Other python3 invocations not matching safe patterns are command_not_allowed
        return (
            "blocked",
            "command_not_allowed",
            "blocked",
            f"python3 invocation '{raw_command}' is not in the VC preflight allowlist; "
            "use python3 -m pytest or python3 -m py_compile",
            "baseline_fail_expected",
        )

    # 6. Check git: exact read-only allowlist (B3)
    if cmd_basename == "git":
        if not _is_allowed_git_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'git {argv[1] if len(argv) > 1 else ''}' is not in the git read-only allowlist; "
                "allowed: status, diff, log, show, ls-files, rev-parse. "
                "git worktree, git -c, and mutation commands are blocked.",
                "baseline_fail_expected",
            )
        return None  # allowed git read-only command

    # 6.5. Check github_metadata_assert: first-class GitHub metadata assertion (Issue #942)
    if _is_github_metadata_assert_command(raw_command):
        is_valid, error_msg = _is_allowed_github_metadata_assert(argv)
        if not is_valid:
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"github_metadata_assert validation failed: {error_msg}",
                "baseline_fail_expected",
            )
        return None  # allowed github_metadata_assert command

    # 7. Check gh: exact read-only allowlist (B3)
    if cmd_basename == "gh":
        if not _is_allowed_gh_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'gh {' '.join(argv[1:])}' is not in the gh read-only allowlist; "
                "allowed: gh issue view, gh pr view, gh pr list, gh issue list, gh repo view. "
                "gh api, gh alias, gh extension, and mutation commands are blocked.",
                "baseline_fail_expected",
            )
        return None  # allowed gh read-only command

    # 8. Check pnpm: exact subcommand allowlist (B1)
    if cmd_basename == "pnpm":
        if not _is_allowed_pnpm_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'pnpm {argv[1] if len(argv) > 1 else ''}' is not in the pnpm allowlist; "
                "only pnpm typecheck, pnpm lint, pnpm test, pnpm build are allowed. "
                "pnpm exec, pnpm dlx, pnpm run, pnpm add, etc. are blocked.",
                "baseline_fail_expected",
            )
        return None  # allowed pnpm subcommand

    # 8.5. Check env: display-only allowlist
    if cmd_basename == "env":
        if not _is_allowed_env_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                "'env' is only allowed for read-only display (env, env --help, env --version). "
                "Do not use env as a wrapper or to inject arbitrary variables into commands. "
                "Shell env prefix forms like 'CI=true pnpm build' are also not allowed.",
                "baseline_fail_expected",
            )
        return None

    # 9. Check uv: only allow uv run pytest / uv run python -m pytest / uv run python3 -m pytest
    if cmd_basename == "uv":
        if len(argv) >= 2 and argv[1] == "lock":
            if _is_uv_lock_check(argv):
                return None
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                "uv lock subcommand in VC preflight is allowed only as 'uv lock --check'.",
                "baseline_fail_expected",
            )

        if len(argv) >= 2 and argv[1] == "run":
            # Runtime smoke canonical allowlist (raw argv exact shape)
            if _is_uv_runtime_smoke_command(argv):
                return None

            unwrapped = _strip_uv_run_options(argv)
            if (
                unwrapped
                and Path(unwrapped[0]).name in ("python", "python3")
                and len(unwrapped) >= 2
                and unwrapped[1] in ("-m", "-c")
                and any(
                    opt in argv
                    for opt in ("--isolated", "--locked", "--no-default-groups")
                )
            ):
                return (
                    "blocked",
                    "command_not_allowed",
                    "blocked",
                    (
                        "runtime-smoke options require the exact canonical script target; "
                        "python -m/-c runtime forms are not allowed."
                    ),
                    "baseline_fail_expected",
                )
            if unwrapped and Path(unwrapped[0]).name in ("pytest",):
                return None  # allowed
            if (
                unwrapped
                and Path(unwrapped[0]).name in ("python", "python3")
                and len(unwrapped) >= 3
                and unwrapped[1] == "-m"
                and unwrapped[2] == "pytest"
            ):
                return None  # allowed

            # uv run <other> is not in allowlist
            inner_cmd = unwrapped[0] if unwrapped else "<unknown>"
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'uv run {inner_cmd}' is not in the VC preflight allowlist; "
                "only 'uv run pytest', 'uv run python3 -m pytest', "
                "'uv run --isolated --locked --no-default-groups python "
                "scripts/ci/runtime_dependency_smoke.py', and "
                "'uv run --isolated --locked --no-default-groups python3 "
                "scripts/ci/runtime_dependency_smoke.py' are allowed.",
                "baseline_fail_expected",
            )
        else:
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"uv subcommand '{argv[1] if len(argv) > 1 else ''}' is not in the VC preflight allowlist",
                "baseline_fail_expected",
            )

    # 10. Default allowlist check: command not in allowed set → blocked (AC3)
    if cmd_basename not in _ALLOWED_COMMANDS:
        return (
            "blocked",
            "command_not_allowed",
            "blocked",
            f"'{cmd_basename}' is not in the VC preflight allowlist; "
            "only explicitly allowed commands are permitted "
            "(rg, test, grep, uv run pytest, pnpm typecheck/lint/test/build, etc.)",
            "baseline_fail_expected",
        )

    return None  # proceed to execution


# Issue #1328: whitelist of stderr patterns that indicate `rg` failed because the
# target PATH does not exist (missing file/directory), as opposed to a
# regex/option/config/permission error.
_RG_STDERR_MISSING_PATH_PATTERNS = (
    re.compile(r"No such file or directory"),
    re.compile(r"\(os error 2\)"),
)

# Issue #1328: blacklist of stderr patterns that indicate `rg` failed for a
# reason OTHER than a missing path (invalid regex / unsupported option /
# config error / permission denied). If any of these match, the failure must
# NOT be treated as a deterministic "missing path" expected_fail, even if a
# missing-path pattern also happens to be present in the same stderr (AC10).
_RG_STDERR_ERROR_NOT_MISSING_PATH_PATTERNS = (
    re.compile(r"regex parse error"),
    re.compile(r"unrecognized flag"),
    re.compile(r"unrecognized option"),
    re.compile(r"error:\s*unclosed"),
    re.compile(r"error:\s*found argument"),
    re.compile(r"error:\s*invalid"),
    re.compile(r"Permission denied"),
    re.compile(r"\(os error 13\)"),
    re.compile(r"bad config"),
)


def _rg_stderr_indicates_missing_path(stderr: str) -> bool:
    """Issue #1328: True if stderr matches a known 'target path does not exist'
    pattern for `rg` (e.g. "No such file or directory (os error 2)")."""
    return any(pattern.search(stderr) for pattern in _RG_STDERR_MISSING_PATH_PATTERNS)


def _rg_stderr_indicates_error_not_missing_path(stderr: str) -> bool:
    """Issue #1328: True if stderr matches a known non-missing-path `rg` error
    pattern (invalid regex / unsupported option / config error / permission
    denied)."""
    return any(pattern.search(stderr) for pattern in _RG_STDERR_ERROR_NOT_MISSING_PATH_PATTERNS)


def _is_rg_missing_path_error(stderr: str) -> bool:
    """Issue #1328: True only if stderr strictly indicates a missing-path error
    (whitelist match) AND does not also match any non-missing-path error
    pattern (blacklist). AC10: whitelist+blacklist co-occurrence returns False
    (conservative; do not treat as deterministic missing-path expected_fail)."""
    return _rg_stderr_indicates_missing_path(stderr) and not _rg_stderr_indicates_error_not_missing_path(stderr)


# Issue #2638: the new preflight category returned by
# `_detect_rg_grep_exit2_syntax_error()` for rg/grep exit_code==2 regex-syntax
# / invalid-option / usage-error VCs. Deliberately a single category name
# used consistently for all 4 bounded subtypes (rg_regex_parse_error /
# rg_invalid_option / grep_regex_error / grep_invalid_option); the subtype
# itself is only surfaced in the fix_hint text, not as a separate category.
VC_GREP_SYNTAX_ERROR_CATEGORY = "vc_grep_syntax_error"

# Issue #2638 P2 fix (PR #2643 review Finding 1): some grep builds echo the
# VERBATIM `argv[0]` as invoked (not its basename) in their diagnostic
# prefix -- e.g. invoking `/usr/bin/grep -E '[' file` on GNU grep 3.11
# locally prints `/usr/bin/grep: Invalid regular expression`, not
# `grep: Invalid regular expression` -- even though the classifier's own
# executable-family detection (`prog = os.path.basename(argv[0])`, below)
# correctly recognizes this as a grep invocation. ripgrep, by contrast, was
# verified locally (14.1.0) to ALWAYS self-identify as the fixed literal
# `rg:` regardless of invocation path (`rg`, `/usr/bin/rg`, etc. all print
# `rg: ...`). To cover both behaviors without guessing which one a given
# build uses, the diagnostic-prefix regex tries exactly 2 bounded
# candidates -- the verbatim `argv[0]` string and its basename -- built
# fresh per invocation from the PARSED argv (never an unbounded wildcard
# such as `.*grep:`, which could false-match unrelated text).
def _stderr_diagnostic_prefix_alternation(argv0: str) -> str:
    """Build a regex alternation (already `re.escape()`d) of the bounded
    diagnostic-prefix candidates for `argv0`: the literal string as invoked
    and its basename. Used as the `{prefix}` slot in the message-line regex
    templates below."""
    candidates = sorted({argv0, os.path.basename(argv0)})
    return "|".join(re.escape(candidate) for candidate in candidates)


# Issue #2638: rg's OWN regex-parse-error stderr format is a multi-line
# message starting with the literal line "rg: regex parse error:" (verified
# against ripgrep 14.1.0 locally), which is structurally distinct from
# grep/egrep/fgrep's single-line "grep: Invalid regular expression" format.
_RG_REGEX_PARSE_ERROR_LINE_TEMPLATE = r"^(?:{prefix}): regex parse error:"

# Issue #2638 (P2 fix, PR #2643 review Finding 2): rg's own invalid-option/
# usage-error stderr formats. "unrecognized flag" / "unrecognized option" /
# "unrecognized file type" / "error parsing flag" were observed locally
# (ripgrep 14.1.0); "missing value for flag" (e.g.
# `rg: missing value for flag --max-count: missing argument for option
# '--max-count'`, from `rg foo file --max-count`) was added after local
# reproduction during PR #2643 review. All are single-line and begin with
# "rg: " (distinct from the multi-line regex-parse-error format above).
# This is a narrow, bounded allowlist -- NOT a reuse of
# `_RG_STDERR_ERROR_NOT_MISSING_PATH_PATTERNS` (that blacklist also matches
# permission/config/env errors such as "Permission denied" / "bad config",
# which must NOT be absorbed into this new body-author-fixable category).
_RG_INVALID_OPTION_MESSAGES = (
    "unrecognized flag",
    "unrecognized option",
    "unrecognized file type",
    "error parsing flag",
    "missing value for flag",
)
_RG_INVALID_OPTION_LINE_TEMPLATE = (
    r"^(?:{prefix}): (?:" + "|".join(re.escape(m) for m in _RG_INVALID_OPTION_MESSAGES) + r")\b"
)

# Issue #2638 (P2 fix, PR #2643 review Finding 2): GNU grep's own
# regex-error stderr formats. "Invalid regular expression" was observed
# locally (GNU grep 3.11, e.g. `grep -E '[' file`); "Unmatched ( or \\("
# (from `grep -E '(' file`) and "Invalid range end" (from
# `grep -E '[z-a]' file`) were added after local reproduction during PR
# #2643 review. egrep/fgrep are symlinks to the same binary and share this
# format, prefixed with their own invoked name instead of "grep:".
_GREP_REGEX_ERROR_MESSAGES = (
    "Invalid regular expression",
    "Unmatched ( or \\(",
    "Invalid range end",
)
_GREP_REGEX_ERROR_LINE_TEMPLATE = (
    r"^(?:{prefix}): (?:" + "|".join(re.escape(m) for m in _GREP_REGEX_ERROR_MESSAGES) + r")"
)

# Issue #2638 (P2 fix, PR #2643 review Finding 2): GNU grep's own
# invalid-option/usage-error stderr formats. "unrecognized option '...'"
# (followed by a "Usage: grep [OPTION]..." banner line) and "invalid
# option --" (used by some grep builds/locales) were observed locally (GNU
# grep 3.11). "option requires an argument" (from `grep foo file -e`) and
# "invalid max count" (from `grep --max-count=no foo file`) were added
# after local reproduction during PR #2643 review.
_GREP_INVALID_OPTION_MESSAGES = (
    "unrecognized option",
    "invalid option",
    "option requires an argument",
    "invalid max count",
)
_GREP_INVALID_OPTION_LINE_TEMPLATE = (
    r"^(?:{prefix}): (?:" + "|".join(re.escape(m) for m in _GREP_INVALID_OPTION_MESSAGES) + r")\b"
)


def _detect_rg_grep_exit2_syntax_error(command: str, exit_code: int, stderr: str) -> Optional[str]:
    """Issue #2638: positively identify exactly 4 bounded rg/grep exit_code==2
    regex-syntax / invalid-option / usage-error patterns via the PARSED argv
    executable basename -- mirroring `_candidate_new_allowed_path_target()`'s
    `os.path.basename(argv[0])` style executable-name detection -- rather
    than a raw substring search over the raw command string (e.g. "rg" or
    "grep" appearing anywhere in the command text, which could false-match
    an unrelated command whose arguments merely contain those substrings).

    Returns one of "rg_regex_parse_error" | "rg_invalid_option" |
    "grep_regex_error" | "grep_invalid_option" if the command's executable
    is rg/grep-family, exit_code == 2, AND stderr matches the corresponding
    bounded positive pattern above; otherwise None (including when exit_code
    != 2, or the executable is not rg/grep/egrep/fgrep, or stderr does not
    match any of the bounded patterns -- e.g. permission/config/env/I/O
    failures, or the Issue #1328 missing-path special case, are never
    absorbed into this category by this function). PR #2643 review Finding
    1: the diagnostic-prefix candidates are derived from THIS invocation's
    own `argv[0]` (verbatim + basename), so `/usr/bin/grep`-style absolute
    invocations are recognized the same as bare `grep`."""
    if exit_code != 2:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    prefix = _stderr_diagnostic_prefix_alternation(argv[0])
    if prog == "rg":
        if re.search(_RG_REGEX_PARSE_ERROR_LINE_TEMPLATE.format(prefix=prefix), stderr, re.MULTILINE):
            return "rg_regex_parse_error"
        if re.search(_RG_INVALID_OPTION_LINE_TEMPLATE.format(prefix=prefix), stderr, re.MULTILINE):
            return "rg_invalid_option"
        return None
    if prog in ("grep", "egrep", "fgrep"):
        if re.search(_GREP_REGEX_ERROR_LINE_TEMPLATE.format(prefix=prefix), stderr, re.MULTILINE):
            return "grep_regex_error"
        if re.search(_GREP_INVALID_OPTION_LINE_TEMPLATE.format(prefix=prefix), stderr, re.MULTILINE):
            return "grep_invalid_option"
        return None
    return None


def _normalize_repo_relative_path_strict(path: str) -> Optional[str]:
    """Issue #1328 (OWNER Blocker 2): strict repo-relative POSIX path
    normalization, mirroring `AllowedPathsMatcher.normalize_path()` in
    `.claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py`.
    Fail-closed: returns None (reject) for absolute paths, paths containing a
    backslash, `..` segments, and empty segments (e.g. `docs//new/file.md`).
    Reimplemented here (not imported) to keep this script's dependency
    surface self-contained; the two normalization rules are covered by
    parity tests and MUST stay in sync."""
    if not path:
        return None
    if "\\" in path:
        return None
    if path.startswith("/"):
        return None

    normalized = path[2:] if path.startswith("./") else path
    if normalized in ("", "."):
        return None
    segments = normalized.split("/")
    if ".." in segments:
        return None
    if "" in segments:
        return None
    return normalized


def _rg_path_operands_all_within_allowed_paths(path_operands: List[str], allowed_paths: List[str]) -> bool:
    """Issue #1328: True iff every rg path operand is equal to, or nested
    under, one of the (normalized) Allowed Paths. Empty path_operands ->
    False (a broad/whole-repo search is never "within" Allowed Paths).

    OWNER Blocker 2: both path operands and Allowed Paths entries are
    normalized via `_normalize_repo_relative_path_strict()` (fail-closed);
    absolute paths, backslashes, `..` segments, and empty segments are
    rejected instead of being accepted via naive string-prefix matching."""
    if not path_operands:
        return False
    norm_allowed: List[str] = []
    for ap in allowed_paths:
        candidate = ap.strip()
        if not candidate:
            continue
        if candidate != "/":
            candidate = candidate.rstrip("/")
        normalized_ap = _normalize_repo_relative_path_strict(candidate)
        if normalized_ap is not None:
            norm_allowed.append(normalized_ap)
    if not norm_allowed:
        return False

    def _in_allowed(p: str) -> bool:
        normalized_p = _normalize_repo_relative_path_strict(p)
        if normalized_p is None:
            return False
        return any(normalized_p == a or normalized_p.startswith(a + "/") for a in norm_allowed)

    return all(_in_allowed(p) for p in path_operands)


# Issue #1328 (OWNER Blocker 1): matches a single `rg` stderr line of the form
# `rg: <path>: No such file or directory (os error 2)`, capturing <path>, so
# the missing path(s) reported by rg can be compared against the argv path
# operands (preventing false-green on multi-path invocations where one path
# matched and produced stdout while a different path was missing).
_RG_STDERR_MISSING_PATH_LINE = re.compile(
    r"^rg:\s*(.+?):\s*No such file or directory(?:\s*\(os error 2\))?\s*$",
    re.MULTILINE,
)


def _rg_stderr_extract_missing_paths(stderr: str) -> List[str]:
    """Issue #1328 (OWNER Blocker 1): extract path(s) that rg reported as
    missing directly from stderr lines matching `_RG_STDERR_MISSING_PATH_LINE`.
    Returns raw (un-normalized) path strings in first-appearance order,
    deduplicated. Returns an empty list if no such line is found."""
    seen: List[str] = []
    for m in _RG_STDERR_MISSING_PATH_LINE.finditer(stderr):
        candidate = m.group(1).strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


# PR #1497 review (Blocker 1): only these commands have a path-operand
# parser implemented for current-head PASS target-scope certification.
# grep / egrep / fgrep / ack / ag / etc. are intentionally excluded until a
# dedicated path parser exists for them (closed allowlist, fail-closed).
_CURRENT_PASS_ELIGIBLE_TEST_FLAGS = frozenset(["-f", "-d", "-s"])


def _certified_path_exists_as_file(resolved_cwd: Path, normalized: str) -> bool:
    try:
        return (resolved_cwd / normalized).is_file()
    except OSError:
        return False


def certify_current_pass_command(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    cwd: Optional[str],
    allowed_paths: Optional[List[str]],
) -> Tuple[bool, Optional[str]]:
    """PR #1497 review fix (Blocker 1 / Blocker 2): decide whether a
    non-regression-gate command's exit_code == 0 in current-head
    evidence-mode may be certified as a resolved current PASS.

    This is a CLOSED allowlist, not a generic "exit 0 means done" check:

    - Blocker 1 (unbound target scope): only `test -f|-d|-s <PATH>` and
      `rg PATTERN <PATH...>` are eligible. Every path operand MUST normalize
      to a strict repo-relative path (`_normalize_repo_relative_path_strict`)
      contained within `allowed_paths`, and MUST exist as a regular file at
      certification time. Absolute paths, `..`, backslashes, and repo-
      external state (e.g. `/tmp/vc-sentinel`) are rejected outright.
      `grep` / `egrep` / `fgrep` have no path-operand parser here and are
      therefore never certified (they keep the pre-existing
      unexpected_pass / blocked classification even in current-head mode).
    - Blocker 2 (quiet partial success): `stderr` must be empty. `rg -q` /
      `grep -q` can exit 0 (a match was found) while ALSO emitting a
      missing-path / permission-denied error for a different operand on
      stderr; requiring empty stderr rejects this "quiet partial success"
      case even when an error-suppression flag would otherwise hide the
      message from a text-pattern check. Filesystem existence is checked
      independently of stderr content for defense in depth.

    Returns (certified, reason). `reason` is a short machine string
    describing the rejection when `certified` is False; `None` when
    `certified` is True.
    """
    if exit_code != 0:
        return False, "exit_code_not_zero"
    if stderr:
        return False, "nonempty_stderr"
    if not allowed_paths:
        return False, "no_allowed_paths"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False, "argv_parse_failed"
    if not argv:
        return False, "empty_argv"

    resolved_cwd = Path(cwd) if cwd else Path(".")
    base = Path(argv[0]).name

    if base == "test":
        if len(argv) != 3 or argv[1] not in _CURRENT_PASS_ELIGIBLE_TEST_FLAGS:
            return False, "test_argv_not_exact_allowlisted_form"
        target = argv[2]
        normalized = _normalize_repo_relative_path_strict(target)
        if normalized is None:
            return False, "test_path_not_repo_relative"
        if not _rg_path_operands_all_within_allowed_paths([target], allowed_paths):
            return False, "test_path_outside_allowed_paths"
        if not _certified_path_exists_as_file(resolved_cwd, normalized):
            return False, "test_path_does_not_exist_at_certification_time"
        return True, None

    if base == "rg":
        path_operands = _rg_extract_path_operands(argv)
        if not path_operands:
            return False, "rg_no_path_operands"
        if not _rg_path_operands_all_within_allowed_paths(path_operands, allowed_paths):
            return False, "rg_path_outside_allowed_paths"
        for operand in path_operands:
            normalized = _normalize_repo_relative_path_strict(operand)
            if normalized is None:
                return False, "rg_path_not_repo_relative"
            if not _certified_path_exists_as_file(resolved_cwd, normalized):
                return False, "rg_path_not_a_regular_file_or_missing"
        return True, None

    # grep / egrep / fgrep / any other command: closed allowlist -- never
    # certified until a dedicated path-operand parser is implemented.
    return False, "command_not_in_current_pass_allowlist"


def _extract_certified_target_paths(command: str) -> List[str]:
    """Best-effort re-extraction of the path operands certified by
    `certify_current_pass_command()`, for inclusion in the producer result
    item (PR #1497 review Blocker 1: "producer 結果に、検証済み target path
    ... を含める"). Only called after certification already succeeded, so
    this does not need to re-validate anything -- it mirrors the same
    per-command extraction rules for observability."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return []
    if not argv:
        return []
    base = Path(argv[0]).name
    if base == "test" and len(argv) == 3:
        return [argv[2]]
    if base == "rg":
        return _rg_extract_path_operands(argv)
    return []


def classify_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    command: str,
    cwd: Optional[str] = None,
    runner_env_delta: Optional[Dict[str, str]] = None,
    allowed_paths: Optional[List[str]] = None,
    static_policy_passed: bool = False,
    evidence_mode: str = "baseline",
) -> Tuple[str, str, str, Optional[str], str]:
    """
    VC 実行結果を分類。

    Args:
        exit_code: command exit code
        stdout: command standard output
        stderr: command standard error
        command: original command string
        cwd: working directory (threaded to _is_regression_gate_command)
        allowed_paths: Issue body ## Allowed Paths list (Issue #1328). Required
            (non-empty) for the deterministic `new_file_missing_expected`
            classification; without it, rg exit_code==2 never resolves to that
            category regardless of stderr content (AC9).
        static_policy_passed: whether classify_static_command() already ran and
            returned None (not statically blocked) for this command. Defaults
            to False (PR #1497 review Major 1: this is a safety-relevant flag
            and must fail closed; callers MUST explicitly pass True after
            confirming classify_static_command() returned None).
        evidence_mode: "baseline" (default, backward compatible) or
            "current-head" (Issue #1488). In current-head mode, a
            non-regression-gate VC (rg / test -f / test -s etc.) that passed
            static policy and exited 0 is classified as a certified current
            PASS (expected_pass / go) instead of unexpected_pass / blocked.
            baseline evidence-mode semantics are unchanged (AC1).

    戻り値: (classification, category, decision, fix_hint, scope_class)
      classification: expected_fail | unexpected_pass | blocked | human_judgment | expected_pass | skipped
      category: file_not_found_expected | expected_baseline_fail | unexpected_pass |
                env_missing_dep | file_not_found_unrunnable | timeout |
                compound_command_disallowed | unsupported_shell_syntax |
                unsafe_command | command_not_allowed | unknown | regression_gate |
                new_file_missing_expected | existing_file_missing_node_id_noncanonical |
                expected_pass_resolved_on_current_head
      decision: go | blocked | human_judgment
      fix_hint: nullable hint
      scope_class: baseline_fail_expected | regression_gate | pr_review_only | runtime_only
    """

    # AC6: negated search commands - static classification (BEFORE run_command)
    if _is_negated_search_command(command):
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # AC1: command substitution detection - blocked (unsupported_shell_syntax), not executed
    if has_command_substitution(command):
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            "Shell substitution ($(...), `...`, ${...}) is not supported in VC preflight; "
            "use a direct command without command substitution",
            "baseline_fail_expected",
        )

    # compound command は blocked (default scope_class)
    if detect_compound_command(command):
        return (
            "blocked",
            "compound_command_disallowed",
            "blocked",
            "Compound shell commands are not supported in initial implementation",
            "baseline_fail_expected"
        )

    # timeout check (Issue #1892: exact canonical runner sentinel only.
    # run_command() sets stderr to the literal string "timeout" and
    # exit_code to -1 exclusively on subprocess.TimeoutExpired; matching on
    # a stderr substring ("timeout" in stderr.lower()) previously caused
    # any non-timeout command whose stderr merely contained the word
    # "timeout" (e.g. a pytest node-id or error message referencing a
    # timeout test) to be misclassified as a real timeout.
    #
    # Issue #2207 AC9/AC10: this check MUST run BEFORE the regression_gate
    # branch below. `_is_regression_gate_command()` matches on command
    # SHAPE only (e.g. any pytest/uv-run-pytest invocation), independent of
    # exit_code -- so a regression-gate-shaped VC that actually timed out
    # (exit_code -1, run_command()'s canonical sentinel) would otherwise
    # fall into the regression_gate branch's generic
    # "Regression gate command failed" classification and NEVER reach this
    # timeout-specific classification, masking a genuine per-command-timeout
    # fault as an ordinary regression-gate failure.
    if exit_code == -1 and stderr.strip() == "timeout":
        return "blocked", "timeout", "blocked", "Command exceeded timeout", "baseline_fail_expected"

    # AC4: regression_gate prefix detection AFTER static checks
    # If it's a regression gate, apply special rules
    if _is_regression_gate_command(command, cwd=cwd):
        if exit_code == 0:
            return "expected_pass", "regression_gate", "go", None, "regression_gate"
        else:
            if _is_package_manager_no_tty_prompt(command, stdout, stderr):
                applied_runner_delta = runner_env_delta or {}
                if applied_runner_delta == {"CI": "true"}:
                    fix_hint = (
                        "CI=true was already injected by the runner. This is still a pnpm/node_modules/tooling "
                        "state blocker; do not rewrite the Issue body."
                    )
                else:
                    fix_hint = (
                        "This is a package manager / no-TTY runner environment blocker, not an Issue body defect. "
                        "Do not weaken ACs or rewrite the pnpm gate to runtime_only. "
                        "Inspect pnpm/node_modules/tooling state; if runner_env_delta is missing, "
                        "retry the same canonical pnpm gate with runner-side CI=true."
                    )
                return (
                    "blocked",
                    "package_manager_no_tty_prompt",
                    "blocked",
                    fix_hint,
                    "regression_gate",
                )
            # B5: Check pytest exit codes 4/5 BEFORE regression_gate failure classification
            if _is_pytest_invocation(command):
                combined_lower = f"{stdout}\n{stderr}".lower()

                # pytest exit 4 + file not found → expected_baseline_fail (env/path missing)
                if exit_code == 4 and re.search(r"error:\s+file or directory not found:", combined_lower):
                    return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

                # Issue #1347: pytest exit 4 where the referenced FILE exists
                # (this branch is only reached when _is_regression_gate_command()
                # already confirmed the file portion exists) but the node-id
                # (test function) does not, e.g.:
                #   ERROR: not found: <path>/existing_file.py::test_new_case
                #   (no match in any of [<Module existing_file.py>])
                # This is a non-canonical VC shape (Issue #1285 / PR #1305:
                # existing-file missing node-id is forbidden; the canonical
                # form is a missing-file node-id). It must NOT be classified
                # as expected_baseline_fail/go, and must be distinguishable
                # from the generic `unknown` / `human_judgment` fallback so
                # diagnostics can identify this specific shape.
                if exit_code == 4 and _is_existing_file_missing_node_id_error(stdout, stderr, cwd=cwd):
                    return (
                        "blocked",
                        "existing_file_missing_node_id_noncanonical",
                        "blocked",
                        "This pytest VC references a NEW test function on an EXISTING "
                        "file (<file>::<new_test_name>), which is a non-canonical VC "
                        "shape (Issue #1285 / PR #1305). Rewrite the VC to reference a "
                        "not-yet-created test file (missing_new_test_file.py::test_name) "
                        "instead of adding a new test function to an existing file.",
                        "baseline_fail_expected",
                    )

                # B5: pytest exit 5 → vc_no_tests_collected / blocked
                # exit 5 is "no tests collected" — -k condition mismatch or wrong path
                if exit_code == 5:
                    return (
                        "blocked",
                        "vc_no_tests_collected",
                        "blocked",
                        "pytest collected 0 tests (exit 5); check -k filter or test path",
                        "baseline_fail_expected",
                    )

            return "blocked", "regression_gate", "blocked", "Regression gate command failed", "regression_gate"

    # Issue #1892: pytest exit_code==4 (missing test file/node-id) and
    # exit_code==5 (no tests collected) are checked BEFORE the generic
    # timeout substring check below, so a fast, correctly-completed
    # baseline fail is never misclassified as a timeout merely because
    # the test node-id/path happens to contain the word "timeout" (e.g.
    # test_agy_real_subprocess_timeout_classified). A real
    # subprocess.TimeoutExpired always reports exit_code == -1 (see
    # run_command()), so it can never match exit_code == 4/5 here and
    # always falls through to the timeout check unaffected.
    # pytest baseline fail patterns (AC2, AC3)
    if _is_pytest_invocation(command):
        combined_lower = f"{stdout}\n{stderr}".lower()

        # pytest exit 4 + file not found → expected_baseline_fail (path/env missing)
        if exit_code == 4 and re.search(r"error:\s+file or directory not found:", combined_lower):
            return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

        # B5: pytest exit 5 → vc_no_tests_collected / blocked
        # exit 5 = no tests collected (-k mismatch, wrong path, etc.) → not a valid baseline VC
        if exit_code == 5:
            return (
                "blocked",
                "vc_no_tests_collected",
                "blocked",
                "pytest collected 0 tests (exit 5); check -k filter or test path",
                "baseline_fail_expected",
            )

    # exit_code = 0 で回帰ゲート以外
    # Issue #1488: current-head evidence-mode では、静的 policy を通過して
    # 実行された非 regression-gate VC（rg / test -f / test -s 等）の exit 0 は
    # certified current PASS（expected_pass / go）として扱う。baseline
    # evidence-mode の意味論（unexpected_pass / blocked）は変更しない（AC1）。
    #
    # PR #1497 review (Blocker 1 / Blocker 2): a bare `exit_code == 0` is NOT
    # sufficient evidence of "this AC was resolved by the reviewed commit".
    # `certify_current_pass_command()` additionally requires: (a) the command
    # is on a closed allowlist (`test -f|-d|-s <PATH>` / `rg PATTERN
    # <PATH...>` only -- grep/egrep/fgrep are excluded until a path-operand
    # parser exists for them), (b) every path operand is a strictly
    # normalized repo-relative path contained within Allowed Paths (rejecting
    # absolute paths / `..` / repo-external state such as /tmp sentinels),
    # (c) every path operand is verified to exist as a regular file at
    # certification time, and (d) stderr is empty (rejecting `rg -q`/`grep
    # -q` "quiet partial success" where one operand matched while another
    # produced a missing-path/permission error that would otherwise be
    # silently absorbed by exit_code == 0 alone).
    if exit_code == 0:
        if evidence_mode == "current-head" and static_policy_passed:
            certified, _reason = certify_current_pass_command(
                command, exit_code, stdout, stderr, cwd, allowed_paths
            )
            if certified:
                return (
                    "expected_pass",
                    "expected_pass_resolved_on_current_head",
                    "go",
                    None,
                    "baseline_fail_expected",
                )
        return "unexpected_pass", "unexpected_pass", "blocked", "Command unexpectedly passed", "baseline_fail_expected"

    # shlex.split failed
    if "shlex.split failed" in stderr:
        return (
            "blocked",
            "compound_command_disallowed",
            "blocked",
            "Command syntax is not supported",
            "baseline_fail_expected"
        )

    # B5: file_not_found_unrunnable - missing script/file being executed
    # e.g., python3 missing.py, node missing.js, ./missing-script
    if (
        ("No such file or directory" in stderr or "can't open file" in stderr)
        and exit_code == 2
        and any(
            cmd_pattern in command
            for cmd_pattern in ["python3 ", "python ", "node ", "./", "../"]
        )
    ):
        return (
            "blocked",
            "file_not_found_unrunnable",
            "blocked",
            "Script or file being executed does not exist",
            "baseline_fail_expected"
        )

    # AC1/AC2/AC3/AC4/AC5/AC9/AC10 (Issue #1328): rg exit_code==2 against a
    # missing file/dir that is fully contained within Allowed Paths, with
    # stderr strictly indicating a "missing path" error (not invalid regex /
    # unsupported option / config error / permission denied), is a
    # deterministic expected_fail. This is a narrow special case: any
    # condition below not satisfied falls through to the existing branches
    # (usually the terminal Unknown fallback -> human_judgment).
    # OWNER Blocker 1: additionally require stdout to be empty (no matches
    # were found anywhere) AND require the stderr-reported missing path(s) to
    # exactly match the full set of argv path operands. Without this, a
    # multi-path invocation where one path matched (non-empty stdout) and a
    # DIFFERENT path was missing would be misclassified as a deterministic
    # baseline fail, even though the VC actually found a match.
    if (
        exit_code == 2
        and static_policy_passed
        and allowed_paths
        and stdout.strip() == ""
    ):
        try:
            _rg_argv_for_missing_path = shlex.split(command)
        except ValueError:
            _rg_argv_for_missing_path = []
        if _rg_argv_for_missing_path and Path(_rg_argv_for_missing_path[0]).name == "rg":
            _rg_path_operands = _rg_extract_path_operands(_rg_argv_for_missing_path)
            if (
                _rg_path_operands_all_within_allowed_paths(_rg_path_operands, allowed_paths)
                and _is_rg_missing_path_error(stderr)
            ):
                _rg_missing_from_stderr = _rg_stderr_extract_missing_paths(stderr)
                _norm_operands = {
                    n for n in (
                        _normalize_repo_relative_path_strict(p) for p in _rg_path_operands
                    ) if n is not None
                }
                _norm_missing = {
                    n for n in (
                        _normalize_repo_relative_path_strict(p) for p in _rg_missing_from_stderr
                    ) if n is not None
                }
                # OWNER Blocker 1: the missing-path set reported by stderr must be
                # non-empty and must equal (not merely overlap with) the full set
                # of path operands -- either a single missing path, or every
                # operand accounted for as missing. This rejects "one operand
                # matched, a different operand was missing" false-green cases.
                if _norm_missing and _norm_operands == _norm_missing:
                    return (
                        "expected_fail",
                        "new_file_missing_expected",
                        "go",
                        None,
                        "baseline_fail_expected",
                    )

    # Issue #2638: rg/grep exit_code==2 regex-syntax / invalid-option /
    # usage-error VCs are a body-author-fixable VC design mistake, not an
    # environment/runtime failure -- classify deterministically instead of
    # falling through to the generic `unknown` / `human_judgment` fallback
    # at the bottom of this function. This check runs AFTER the
    # `new_file_missing_expected` special case above (Issue #1328) so a
    # genuine missing-Allowed-Path rg invocation is never reclassified here
    # (its stderr, "No such file or directory", never matches any of the 4
    # bounded regex-syntax/invalid-option patterns below).
    _syntax_error_subtype = _detect_rg_grep_exit2_syntax_error(command, exit_code, stderr)
    if _syntax_error_subtype is not None:
        return (
            "blocked",
            VC_GREP_SYNTAX_ERROR_CATEGORY,
            "blocked",
            f"VC command failed with a recognized rg/grep syntax/usage error "
            f"({_syntax_error_subtype}, exit code 2). This is a body-author-fixable "
            "VC design mistake (invalid regex pattern or unsupported command-line "
            "option), not an environment/runtime failure -- fix the regex pattern "
            "or option usage in the Verification Command.",
            "baseline_fail_expected",
        )

    # env_missing_dep: command not found (127), permission denied (126), ModuleNotFoundError, etc.
    if exit_code in (126, 127):
        return (
            "blocked",
            "env_missing_dep",
            "blocked",
            "Command not found or permission denied",
            "baseline_fail_expected"
        )

    if "command not found" in stderr.lower() or "ModuleNotFoundError" in stderr:
        return "blocked", "env_missing_dep", "blocked", "Dependency or command missing", "baseline_fail_expected"

    if "Permission denied" in stderr:
        return "blocked", "env_missing_dep", "blocked", "Permission denied", "baseline_fail_expected"

    if "No such file or directory" in stderr and exit_code == -1:
        return "blocked", "env_missing_dep", "blocked", "Command not found", "baseline_fail_expected"

    # expected baseline fail patterns
    # rg with no match returns 1
    if "rg " in command and exit_code == 1:
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # test -f / test -d / test -s with non-existent or zero-size file
    # Use shlex.split for argv parse instead of substring matching (AC5)
    try:
        _test_argv = shlex.split(command)
    except ValueError:
        _test_argv = []
    # Handle test -s exit 2 (malformed) regardless of argument count (AC7)
    # Must come before the len==3 guard so `test -s` (no operand, len=2) is also caught.
    if (
        len(_test_argv) >= 2
        and _test_argv[0] == "test"
        and _test_argv[1] == "-s"
        and exit_code == 2
    ):
        return (
            "blocked",
            "unknown",
            "blocked",
            "test -s returned exit 2 (malformed invocation)",
            "baseline_fail_expected"
        )
    # Exact 3-argument predicate: test <flag> <path> — no extra operands allowed (AC5)
    if len(_test_argv) == 3 and _test_argv[0] == "test":
        _flag = _test_argv[1]
        if _flag in ("-f", "-d") and exit_code == 1:
            return "expected_fail", "file_not_found_expected", "go", None, "baseline_fail_expected"
        if _flag == "-s" and exit_code == 1:
            # exit 1: file missing or zero-size — both are expected baseline fail (AC1, AC2)
            return (
                "expected_fail",
                "file_not_found_expected",
                "go",
                "test -s false means missing or zero-size file",
                "baseline_fail_expected",
            )

    # Generic exit_code != 0: try to infer expected_fail for common utilities
    # grep, sed, awk などが no-match で exit 1 を返すことは expected
    # ただし invalid regex や file not found は exclude (medium risk 1)
    if exit_code == 1 and any(util in command for util in ["grep", "rg", "ag", "ack", "fgrep", "egrep"]):
        # grep が invalid regex で fail した場合 expected_fail にしない
        # "grep:" error pattern を検出
        grep_error_patterns = [
            r"grep:.+: No such file or directory",
            r"grep:.+: Invalid regular expression",
            r"egrep:.+",
            r"fgrep:.+"
        ]
        is_likely_grep_error = any(re.search(pattern, stderr) for pattern in grep_error_patterns)
        if is_likely_grep_error:
            return (
                "human_judgment",
                "unknown",
                "human_judgment",
                "grep error classification uncertain",
                "baseline_fail_expected"
            )
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # Unknown: cannot classify
    return (
        "human_judgment",
        "unknown",
        "human_judgment",
        "Unable to automatically classify exit code",
        "baseline_fail_expected"
    )


def compute_source_hash(body: str) -> str:
    """body の SHA-256 hash を計算"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def compute_confidence(category: str) -> str:
    """
    B8: category に基づいて confidence を算出.

    高確度: compound_command_disallowed, unsupported_shell_syntax, unsafe_command, command_not_allowed,
            file_not_found_expected, expected_baseline_fail, env_missing_dep, file_not_found_unrunnable
    中確度: timeout, unexpected_pass
    低確度: unknown
    """
    high_confidence = {
        "compound_command_disallowed",
        "unsupported_shell_syntax",
        "unsafe_command",
        "command_not_allowed",
        "file_not_found_expected",
        "expected_baseline_fail",
        "env_missing_dep",
        "file_not_found_unrunnable",
        "vc_no_tests_collected",
        "trivially_pass",
        "regex_literal_pipe_suspected",  # AC3: Issue #589
        "rg_option_mismatch",            # AC1: Issue #648
        "broad_search_path_unbounded",   # AC2: Issue #648
        "new_file_missing_expected",     # AC11: Issue #1328
        "expected_pass_resolved_on_current_head",  # PR #1497 review Major 2: Issue #1488
        VC_GREP_SYNTAX_ERROR_CATEGORY,   # Issue #2638: bounded rg/grep exit_code==2 syntax/usage error
    }
    medium_confidence = {"timeout", "unexpected_pass"}

    if category in high_confidence:
        return "high"
    elif category in medium_confidence:
        return "medium"
    else:
        return "low"


def check_c13_vc_preflight_decision_consistency(classifications: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    B4: Validate classifications for schema consistency.

    Checks:
    - All required fields present (classification, scope_class, decision)
    - Enum values valid
    - Cross-field consistency (e.g., skipped requires scope_class pr_review_only or runtime_only)

    Returns: (is_valid, list_of_failures)
    """
    VALID_SCOPE_CLASSES = {"baseline_fail_expected", "regression_gate", "pr_review_only", "runtime_only"}
    VALID_CLASSIFICATIONS = {
        "expected_fail",
        "expected_pass",
        "unexpected_pass",
        "blocked",
        "human_judgment",
        "skipped"
    }
    VALID_DECISIONS = {"go", "blocked", "human_judgment"}

    failures = []

    for i, item in enumerate(classifications):
        prefix = f"classifications[{i}]"

        # Required fields
        if "classification" not in item:
            failures.append(f"{prefix}: missing classification")
        if "scope_class" not in item:
            failures.append(f"{prefix}: missing scope_class")
        if "decision" not in item:
            failures.append(f"{prefix}: missing decision")

        # Enum validation
        if "scope_class" in item and item["scope_class"] not in VALID_SCOPE_CLASSES:
            failures.append(f"{prefix}: invalid scope_class '{item['scope_class']}' (valid: {VALID_SCOPE_CLASSES})")
        if "classification" in item and item["classification"] not in VALID_CLASSIFICATIONS:
            failures.append(
                f"{prefix}: invalid classification"
                f" '{item['classification']}' (valid: {VALID_CLASSIFICATIONS})"
            )
        if "decision" in item and item["decision"] not in VALID_DECISIONS:
            failures.append(f"{prefix}: invalid decision '{item['decision']}' (valid: {VALID_DECISIONS})")

        # Cross-field consistency rules
        if "classification" in item and "scope_class" in item:
            # skipped requires scope_class pr_review_only or runtime_only
            if item["classification"] == "skipped" and item["scope_class"] not in {"pr_review_only", "runtime_only"}:
                failures.append(f"{prefix}: skipped requires scope_class pr_review_only or runtime_only")

        # regression_gate consistency
        if "scope_class" in item and item["scope_class"] == "regression_gate":
            if "decision" in item and "classification" in item:
                if item["decision"] == "go" and item["classification"] != "expected_pass":
                    failures.append(f"{prefix}: regression_gate + go requires classification expected_pass")
                if item["decision"] == "blocked" and item["classification"] != "blocked":
                    failures.append(f"{prefix}: regression_gate + blocked requires classification blocked")

        # PR #1497 review Major 2: expected_pass_resolved_on_current_head
        # consistency (classification / decision / confidence must agree;
        # an "unknown category" fallback to confidence: low would otherwise
        # silently contradict a certified current PASS).
        if item.get("category") == "expected_pass_resolved_on_current_head":
            if item.get("classification") != "expected_pass":
                failures.append(
                    f"{prefix}: expected_pass_resolved_on_current_head requires classification expected_pass"
                )
            if item.get("decision") != "go":
                failures.append(
                    f"{prefix}: expected_pass_resolved_on_current_head requires decision go"
                )
            if "confidence" in item and item["confidence"] != "high":
                failures.append(
                    f"{prefix}: expected_pass_resolved_on_current_head requires confidence high"
                )

        # Skipped routing metadata
        if "classification" in item and item["classification"] == "skipped":
            for k in ("verification_owner", "deferred_reason", "runtime_verification_required"):
                if k not in item:
                    failures.append(f"{prefix}: skipped requires {k}")

    return len(failures) == 0, failures


def generate_contract_review_fragment(status: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    B8: JSON results を CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight に対応する YAML fragment に変換.

    C3: human_judgment を保持する。vc_preflight.passed は status == "pass" のときのみ true。

    B1: Each classification item includes:
      - classification (always)
      - scope_class (always)
      - For skipped items: verification_owner, deferred_reason, runtime_verification_required

    戻り値は YAML 形式の dict.
    """
    vc_failed_as_expected = sum(
        1 for r in results if r["classification"] == "expected_fail"
    )
    vc_passed_unexpectedly = sum(
        1 for r in results if r["classification"] == "unexpected_pass"
    )
    vc_unrunnable = sum(
        1 for r in results if r["classification"] == "blocked"
    )
    vc_human_judgment = sum(
        1 for r in results if r["decision"] == "human_judgment"
    )
    vc_expected_pass = sum(
        1 for r in results if r["classification"] == "expected_pass"
    )
    vc_skipped = sum(
        1 for r in results if r["classification"] == "skipped"
    )

    classifications = []
    for r in results:
        stdout_lines = r["stdout_head"]
        stderr_lines = r["stderr_head"]

        # C3: decision をそのまま渡す（human_judgment を保持）
        decision = r["decision"]

        # B1: classification and scope_class are always included
        classification_item = {
            "ac": r["ac"],
            "command": r["raw_command"],
            "exit_code": r["exit_code"],
            "classification": r["classification"],
            "category": r["category"],
            "confidence": compute_confidence(r["category"]),
            "scope_class": r["scope_class"],
            "evidence": {
                "stdout_excerpt": " ".join(stdout_lines[:5]) if stdout_lines else "",
                "stderr_excerpt": " ".join(stderr_lines[:5]) if stderr_lines else "",
                "runner_env_delta": r.get("runner_env_delta", {}),
            },
            "decision": decision,
        }

        # B1: For skipped items, add routing metadata
        if r["classification"] == "skipped":
            if "verification_owner" in r:
                classification_item["verification_owner"] = r["verification_owner"]
            if "deferred_reason" in r:
                classification_item["deferred_reason"] = r["deferred_reason"]
            if "runtime_verification_required" in r:
                classification_item["runtime_verification_required"] = r["runtime_verification_required"]

        classifications.append(classification_item)

    return {
        "vc_preflight": {
            "passed": status == "pass",
            "vc_failed_as_expected": vc_failed_as_expected,
            "vc_passed_unexpectedly": vc_passed_unexpectedly,
            "vc_unrunnable": vc_unrunnable,
            "vc_human_judgment": vc_human_judgment,
            "vc_expected_pass": vc_expected_pass,
            "vc_skipped": vc_skipped,
            "classifications": classifications,
        }
    }


def _build_result_item(
    *,
    ac_label,
    line_no,
    command,
    exit_code,
    classification,
    category,
    decision,
    scope_class,
    stdout,
    stderr,
    duration_ms,
    fix_hint,
    runner_env_delta,
    preflight_scope,
    baseline_expect,
    vc_role,
    annotation_line_no,
    annotation_raw,
    verification_owner,
    deferred_reason,
    runtime_verification_required,
    strict_enabled,
    max_head_lines,
    execution_key_hash: Optional[str] = None,
    is_dedup_replay: bool = False,
    source_command_hash: Optional[str] = None,
    source_execution_key_hash: Optional[str] = None,
    source_result_index: Optional[int] = None,
    source_ac: Optional[str] = None,
    source_line: Optional[int] = None,
    certified_target_paths: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    timeout_provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one VC result entry.

    Issue #1338: extracted from the main per-command loop so the immediate
    (single-execution) code path and the deferred dedup/bounded-parallel
    execution phase share byte-identical result-shaping logic. New in #1338:
    `execution_key_hash` (dedup key, distinct from the pre-existing
    `command_hash`) and, for dedup-replayed entries, a `dedup` object carrying
    the source AC's command_hash/execution_key_hash. classification/decision
    are always computed by the caller from THIS entry's own
    command/annotations -- never copied from another AC's result.
    """
    stdout_head, stdout_truncated, stdout_orig_count = truncate_output(stdout, max_head_lines)
    stderr_head, stderr_truncated, stderr_orig_count = truncate_output(stderr, max_head_lines)

    runner_value = (
        "skipped" if preflight_scope is not None
        else ("dedup_replay" if is_dedup_replay else "exec")
    )

    result_item = {
        "ac": ac_label or "AC_UNKNOWN",
        "line": line_no,
        "raw_command": command,
        "command_hash": f"sha256:{compute_command_hash(command)}",
        "execution_key_hash": (
            f"sha256:{execution_key_hash}" if execution_key_hash is not None else None
        ),
        "runner": runner_value,
        "exit_code": exit_code,
        "classification": classification,
        "category": category,
        "decision": decision,
        "scope_class": scope_class,
        "confidence": compute_confidence(category),  # C4: category ベースで統一
        "stdout_head": stdout_head,
        "stdout_truncated": stdout_truncated,
        "stdout_original_line_count": stdout_orig_count,
        "stderr_head": stderr_head,
        "stderr_truncated": stderr_truncated,
        "stderr_original_line_count": stderr_orig_count,
        "runner_env_delta": runner_env_delta,
        "duration_ms": duration_ms,
        "fix_hint": fix_hint,
        "certified_target_paths": certified_target_paths,
        "annotations": {
            "baseline_expect": (
                # Expose raw invalid value (without __invalid__: prefix) in annotations
                baseline_expect[len("__invalid__:"):] if (
                    preflight_scope is None
                    and baseline_expect is not None
                    and baseline_expect.startswith("__invalid__:")
                ) else (baseline_expect if preflight_scope is None else None)
            ),
            "vc_role": vc_role if preflight_scope is None else None,
            "missing_baseline_expect": (
                preflight_scope is None
                and baseline_expect is None
                and classification == "unexpected_pass"
            ),
        },
        "annotation_source": {
            "line": annotation_line_no,
            "raw": annotation_raw,
        },
        "strict": {
            "enabled": strict_enabled,
            "violation": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "body_author_fixable": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "needs_fix": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "structured_feedback": {
                "category": category,
                "body_author_fixable": True,
                "category_wide_remediation": True,
            } if category in [(
                "inline_baseline_expect_invalid_placement"
            ), "missing_baseline_expect_for_new_allowed_path"] else None,
        } if strict_enabled or category in [(
            "inline_baseline_expect_invalid_placement"
        ), "missing_baseline_expect_for_new_allowed_path"] else None,
        # strict + repair coordination for inline_baseline_expect and missing_baseline_expect
        "repair": {
            "repairable": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "kind": (
                (
                    "move_inline_baseline_expect_to_preceding_line"
                ) if category == "inline_baseline_expect_invalid_placement"
                else "insert_baseline_expect_fail" if category == "missing_baseline_expect_for_new_allowed_path"
                else None
            ),
            "line_start": line_no,
            "line_end": line_no,
            "reason": (
                (
                    "baseline-expect annotation must be in contiguous preceding comment block"
                ) if category == "inline_baseline_expect_invalid_placement"
                else (
                    "new Allowed Path file requires baseline-expect: fail annotation"
                ) if category == "missing_baseline_expect_for_new_allowed_path"
                else None
            ),
        } if category in [(
            "inline_baseline_expect_invalid_placement"
        ), "missing_baseline_expect_for_new_allowed_path"] else None,
    }
    # "strict" + "repair" payload for inline_baseline_expect and missing_baseline_expect errors
    # AC5: Add routing metadata for skipped results
    if verification_owner:
        result_item["verification_owner"] = verification_owner
        result_item["deferred_reason"] = deferred_reason
        result_item["runtime_verification_required"] = runtime_verification_required

    try:
        _result_argv = shlex.split(command)
    except ValueError:
        _result_argv = []
    pnpm_gate_evidence = _pnpm_gate_evidence_for_command(command, cwd or ".")
    if pnpm_gate_evidence is not None:
        result_item["pnpm_gate_evidence_required"] = True
        result_item["pnpm_gate_evidence"] = pnpm_gate_evidence
    elif pnpm_gate_registry.gate_for_request(_result_argv) is not None:
        # Consumers must fail closed if this producer-declared requirement is
        # present but its companion evidence is missing or malformed.
        result_item["pnpm_gate_evidence_required"] = True

    if timeout_provenance is not None:
        # Issue #2233 AC6: effective per-command timeout provenance, sourced
        # from this command's own `command_timeout_budget/v1` plan entry
        # (never a copy of another command's budget).
        result_item["timeout_provenance"] = timeout_provenance

    if is_dedup_replay:
        # AC2/AC3: dedup replay carries provenance of the source execution,
        # distinct from this AC's own command_hash/classification/decision.
        # PR #1508 review (P2-2): source_result_index/source_ac/source_line
        # let a consumer uniquely locate the exact source AC in `results`
        # (command_hash/execution_key_hash alone cannot disambiguate when
        # multiple ACs would legitimately share the same hash).
        result_item["dedup"] = {
            "source_command_hash": source_command_hash,
            "source_execution_key_hash": (
                f"sha256:{source_execution_key_hash}"
                if source_execution_key_hash is not None else None
            ),
            "source_result_index": source_result_index,
            "source_ac": source_ac,
            "source_line": source_line,
        }

    return result_item


def bounded_worker_count(value: str) -> int:
    """argparse type for --max-workers (Issue #1338, PR #1508 review P2-1):
    only accepts integers in the closed range [1, 8]. Values <= 0, non-
    integer strings, and values > 8 are rejected at argument-parsing time
    (fail-closed bound on VC-preflight concurrency width)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--max-workers must be an integer, got {value!r}")
    if parsed < 1 or parsed > 8:
        raise argparse.ArgumentTypeError(
            f"--max-workers must be between 1 and 8 (inclusive), got {parsed}"
        )
    return parsed


def _main_impl() -> int:
    parser = argparse.ArgumentParser(
        description="Baseline VC Preflight: extract and classify VCs from Issue body"
    )
    parser.add_argument("--issue", type=int, help="GitHub Issue number")
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repo (owner/name); defaults to the trusted slug from the identity config",
    )
    parser.add_argument("--body-file", help="Path to Issue body file (for testing)")
    parser.add_argument("--cwd", default=None, help="Working directory for command execution")
    parser.add_argument(
        "--evidence-mode",
        choices=["baseline", "current-head"],
        default="baseline",
        help="Evidence contract: backward-compatible baseline or producer-certified current-head",
    )
    parser.add_argument(
        "--reviewed-head-sha",
        default=None,
        help="Immutable commit selected by the caller; required in current-head mode",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Timeout per command",
    )
    parser.add_argument(
        "--expected-plan-digest",
        default=None,
        help=(
            "Issue #2233 fix_delta P0-1: a canonical VC plan_digest a parent "
            "process already computed from the SAME body. This process "
            "recomputes the plan from its OWN --body-file/--issue body and "
            "rejects (before launching any subprocess) if the digests do not "
            "match."
        ),
    )
    parser.add_argument(
        "--history-snapshot-file",
        default=None,
        help=(
            "Issue #2254 AC1: path to an immutable history_snapshot/v1 JSON "
            "file a parent process already produced (vc_runtime_history."
            "write_history_snapshot_file()). When given, this process loads "
            "that EXACT snapshot instead of producing its own, keeping "
            "plan_digest convergent across the subprocess boundary. When "
            "omitted, this process produces its own snapshot (standalone / "
            "direct invocation)."
        ),
    )
    parser.add_argument(
        "--no-history-estimator",
        action="store_true",
        default=False,
        help=(
            "Issue #2254: disable the history-based estimator entirely for "
            "this invocation (no snapshot production, no sample recording). "
            "Behavior is then byte-for-byte identical to pre-#2254 "
            "(static_policy/static_fallback only)."
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=bounded_worker_count,
        default=1,
        help=(
            "Issue #1338: bounded parallel execution width (1-8 inclusive) for safe "
            "read-only VC predicates (rg with a validated bounded path operand, exact "
            "test -f|-d|-s PATH). grep/egrep/fgrep are intentionally excluded (PR #1508 "
            "review P0-2). Default 1 preserves fully serial execution and output "
            "identical to pre-#1338 behavior."
        ),
    )
    parser.add_argument("--max-head-lines", type=int, default=20, help="Max lines for stdout/stderr")
    # B8: contract-review-fragment format support
    parser.add_argument(
        "--format",
        choices=["json", "contract-review-fragment"],
        default="json",
        help="Output format (json or contract-review-fragment YAML)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Enable strict mode for annotation enforcement (detect missing annotations as needs_fix)"
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        default=False,
        help=(
            "#993: Static-only mode. Parse VC section for non-canonical inputs "
            "(unlabeled fence, colon AC marker, non-$ command) and return "
            "blocked results without executing any commands."
        ),
    )

    args = parser.parse_args()
    if args.repo is None:
        try:
            args.repo = _repo_identity.load_trusted_repo_slug()
        except _repo_identity.RepositoryIdentityError as exc:
            print(f"repository identity unavailable: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            trusted = _repo_identity.is_trusted_repo(args.repo)
        except _repo_identity.RepositoryIdentityError as exc:
            print(f"repository identity unavailable: {exc}", file=sys.stderr)
            return 2
        if not trusted:
            print(f"repository identity mismatch: --repo {args.repo!r}", file=sys.stderr)
            return 2
    cwd_supplied = args.cwd is not None
    args.cwd = args.cwd or "."
    current_evidence: Optional[Dict[str, Any]] = None

    # Issue #2207 AC9: install cooperative SIGTERM cancellation so an outer
    # caller terminating THIS process (e.g. an aggregate-deadline wrapper)
    # cannot leave in-flight VC subprocess process groups orphaned. POSIX
    # process-group semantics are required; on a platform without them this
    # is intentionally left uninstalled (callers relying on AC9 behavior
    # must treat that as `environment blocked`, not a silent no-op).
    if posix_process_groups_supported():
        install_sigterm_cooperative_cancellation()

    if args.evidence_mode == "current-head":
        invalid_reason = None
        if not cwd_supplied:
            invalid_reason = "current-head requires --cwd"
        elif not args.reviewed_head_sha or isinstance(args.reviewed_head_sha, bool):
            invalid_reason = "current-head requires --reviewed-head-sha"
        elif args.format != "json":
            invalid_reason = "current-head requires --format json"
        elif args.static_only:
            invalid_reason = "current-head cannot be combined with --static-only"
        if invalid_reason:
            current_evidence = {
                "reviewed_head_sha": args.reviewed_head_sha,
                "fallback_detected": False,
                "human_review_required": False,
                "stop_condition_triggered": True,
                "errors": [invalid_reason],
            }
            emit_json(
                current_head_blocked_result(args, current_evidence, invalid_reason),
                args.evidence_mode,
                current_evidence,
            )
            return 1
        current_evidence = collect_current_head_evidence(args.cwd, args.reviewed_head_sha)
        if not current_evidence["certified"]:
            emit_json(
                current_head_blocked_result(args, current_evidence, "current-head certification failed"),
                args.evidence_mode,
                current_evidence,
            )
            return 1

    # Issue body を取得 (C2: error code と一緒に返す)
    body = None
    source_kind = None
    error_code = None

    if args.body_file:
        body, error_code = read_body_file(args.body_file)
        source_kind = "body_file"
    elif args.issue:
        body, error_code = get_issue_body(args.issue, args.repo)
        source_kind = "github_issue"

    if not body:
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {"kind": "unknown", "body_sha256": ""},
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": 1,
            },
            "results": [],
            "errors": [
                {
                    "kind": "retrieval_error",
                    "rule": "VC000_BODY_RETRIEVAL_FAILED",
                    "message": error_code or "failed_to_retrieve_issue_body",
                    "minimal_context": f"issue={args.issue}, repo={args.repo}",
                    "fix_hint": "Check GitHub credentials (gh auth status) and issue number",
                }
            ],
            "diagnostic_report": not_computed_diagnostic_report(),
        }
        emit_json(result, args.evidence_mode, current_evidence)
        # C2: exit code 2 for retrieval/parse errors
        return 2 if error_code else 2

    # Verification Commands セクションを抽出
    #
    # #2782: presence 判定（heading 不在）と content validation（heading は
    # あるが実行可能 content がない）を分離する。旧実装は `if not vc_section:`
    # により、heading 不在（None）と「heading は存在するが本文が空」（""）を
    # 同一の VC001_NO_VERIFICATION_COMMANDS_SECTION（"body does not contain
    # heading"）に conflate していた。ここでは presence（`is None`）のみを
    # 判定し、"" の場合はこの分岐を通過させて、下流の
    # extract_fenced_bash_blocks() / commands 抽出ロジックに空文字列を渡す
    # （結果として VC002_NO_COMMANDS_EXTRACTED 等の content validation エラー
    # に正しく帰着する）。
    vc_section = extract_verification_commands_section(body)
    if vc_section is None:
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": 1,
            },
            "results": [],
            "errors": [
                {
                    "kind": "extraction_error",
                    "rule": "VC001_NO_VERIFICATION_COMMANDS_SECTION",
                    "message": "Verification Commands section not found",
                    "minimal_context": "body does not contain '## Verification Commands' heading",
                    "fix_hint": "Add a '## Verification Commands' section with fenced bash blocks to the Issue body",
                }
            ],
            "diagnostic_report": not_computed_diagnostic_report(),
        }
        emit_json(result, args.evidence_mode, current_evidence)
        # C2: exit code 2 for extraction errors
        return 2

    # #993: --static-only mode: parse for non-canonical VC inputs without executing
    if getattr(args, "static_only", False):
        static_parse = parse_verification_commands_section(vc_section)
        static_results = []
        for se in static_parse.static_errors:
            static_results.append({
                "ac": None,
                "command": se.raw_line,
                "classification": "blocked",
                "category": se.kind,
                "decision": "blocked",
                "exit_code": None,
                "errors": [
                    {
                        "kind": se.kind,
                        "rule": se.rule_id or f"VC_STATIC_{se.kind.upper()}",
                        "message": se.fix_hint,
                        "minimal_context": se.raw_line,
                        "fix_hint": se.fix_hint,
                    }
                ],
            })
        static_status = "blocked" if static_parse.static_errors else "ok"
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "mode": "static_only",
            "status": static_status,
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": len(static_results),
                "human_judgment": 0,
                "extraction_errors": 0,
            },
            "results": static_results,
            "errors": [],
            "diagnostic_report": not_computed_diagnostic_report(),
        }
        emit_json(result, args.evidence_mode, current_evidence)
        return 0 if static_status == "ok" else 1

    # AC2: parse Allowed Paths from Issue body for containment-based broad path detection
    allowed_paths_from_body = extract_allowed_paths(body)

    # Issue #2788 AC1/AC6/AC7: normal execution now consumes the SAME
    # shared-parser-backed adapter `--static-only` mode (above) and
    # `compute_canonical_vc_plan()` use, instead of the legacy
    # `extract_fenced_bash_blocks()` + `parse_commands_from_block()` grammar
    # -- so all three paths share ONE canonical VC command grammar
    # authority and never drift on what counts as a runnable command.
    commands, _vc_parse_result = _command_entries_from_shared_parser(vc_section)

    # Issue #2788 AC1/AC3/AC13: reject a body containing an explanatory
    # non-`$` line inside a ```bash fence BEFORE any subprocess candidate is
    # built -- consistent with `--static-only` mode's rejection of the SAME
    # shared-parser `non_dollar_command` error (AC6), instead of silently
    # ignoring the malformed line while still executing the other commands.
    #
    # Only `kind == "non_dollar_command"` is treated as a whole-body
    # extraction rejection here. Every OTHER `VcParseResult.static_errors`
    # kind corresponds to a line that IS still added to `.commands` (or, for
    # `unlabeled_fence`, is handled by the pre-existing
    # `find_unlabeled_fenced_blocks()` branch below with unchanged messaging)
    # and is deliberately left to per-command classification instead of a
    # blanket rejection, preserving existing behavior:
    #   - `colon_marker` / `suffixed_marker`: the marker itself is invalid,
    #     but the command line that follows it is still extracted (with its
    #     `ac_label` simply not updated) -- exactly like legacy
    #     `parse_commands_from_block()` silently ignored a malformed marker
    #     without blocking the command it precedes.
    #   - `compound_shell`: the `$`-prefixed command IS extracted into
    #     `.commands`; `classify_static_command()` (via `detect_compound_command()`,
    #     now the SAME shared quote-aware primitive, AC9) already classifies
    #     THIS SPECIFIC result as `compound_command_disallowed` per-command
    #     -- rejecting the whole body here would suppress that existing,
    #     more precise per-command diagnostic.
    #   - `inline_backtick`: detected OUTSIDE bash fences entirely (stray
    #     prose elsewhere in the section) and does not correspond to any
    #     extracted command.
    #   - `preceding_marker_with_inline_suffix`: the command is still
    #     extracted; this is an additional advisory-only marker conflict.
    _non_canonical_errors = [
        se for se in _vc_parse_result.static_errors if se.kind == "non_dollar_command"
    ]
    if _non_canonical_errors:
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": len(_non_canonical_errors),
            },
            "results": [],
            "errors": [
                {
                    "kind": "extraction_error",
                    "rule": f"VC004_{se.kind.upper()}",
                    "message": se.fix_hint,
                    "minimal_context": se.raw_line,
                    "fix_hint": se.fix_hint,
                }
                for se in _non_canonical_errors
            ],
            "diagnostic_report": not_computed_diagnostic_report(),
        }
        emit_json(result, args.evidence_mode, current_evidence)
        # C2: exit code 2 for extraction errors
        return 2

    # B3: 0 件抽出は blocked として返す
    if not commands:
        # B4: check for unlabeled fences to provide better error message
        unlabeled = find_unlabeled_fenced_blocks(vc_section)
        if unlabeled:
            no_cmd_error = {
                "kind": "unsupported_vc_format",
                "rule": "VC003_UNLABELED_FENCE_BLOCK",
                "message": "Unlabeled fenced code blocks (```) found in Verification Commands section; "
                           "only ```bash fenced blocks are the canonical VC format",
                "minimal_context": "unlabeled fence blocks are not extracted as VC commands",
                "fix_hint": "Change ``` to ```bash in Verification Commands fenced blocks; "
                            "```bash ... ``` is the canonical VC format",
            }
        else:
            no_cmd_error = {
                "kind": "extraction_error",
                "rule": "VC002_NO_COMMANDS_EXTRACTED",
                "message": "No verification commands extracted from Verification Commands section",
                "minimal_context": "fenced bash blocks found but no commands extracted",
                "fix_hint": "Add '$ <command>' lines inside fenced bash blocks in the Verification Commands section; "
                            "fenced bash blocks (```bash ... ```) are the canonical VC format",
            }
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": 1,
            },
            "results": [],
            "errors": [no_cmd_error],
            "diagnostic_report": not_computed_diagnostic_report(),
        }
        emit_json(result, args.evidence_mode, current_evidence)
        # C2: exit code 2 for extraction errors
        return 2

    # 各コマンドを実行して分類
    # Issue #1338: results is pre-sized so a contiguous parallel batch (below)
    # can write finalized entries back at their original Issue-body command
    # position, regardless of execution completion order (AC7).
    results: List[Optional[Dict[str, Any]]] = [None] * len(commands)
    summary = {
        "expected_fail": 0,
        "unexpected_pass": 0,
        "blocked": 0,
        "human_judgment": 0,
        "expected_pass": 0,
        "skipped": 0,
        "extraction_errors": 0,
    }

    # -----------------------------------------------------------------
    # Issue #2233 AC1/AC2/AC4/AC5/AC6 (fix_delta P0-1): command-level
    # timeout budget, sourced from the SAME `compute_canonical_vc_plan()`
    # producer `contract_readiness_check.py` / `run_root_review_pipeline.py`
    # / `run_contract_review_once.py` call -- this executor no longer
    # independently re-derives a `_command_budgets_by_hash` dict of its own.
    #
    # `--timeout-seconds`, when explicitly passed on the CLI, is a HARD
    # GLOBAL override applied to every command (AC4); otherwise each
    # command gets its own `command_timeout_budget/v1` entry (`static_policy`
    # for a trusted-curated slow command, `static_fallback` otherwise --
    # Issue #2233 fix_delta P0-2). Every command's budget is computed
    # BEFORE any subprocess is launched, so a per-command or aggregate
    # policy-ceiling violation (AC5) is rejected up front rather than
    # surfacing mid-execution.
    #
    # `--expected-plan-digest`, when passed by a parent process that already
    # computed this SAME plan from this SAME body (fix_delta P0-1), is
    # verified BEFORE any subprocess is launched: a mismatch (body drift,
    # tampering, or a stale/wrong digest) is a typed non-retryable rejection.
    # -----------------------------------------------------------------
    _explicit_timeout_override = any(
        _a == "--timeout-seconds" or _a.startswith("--timeout-seconds=")
        for _a in sys.argv[1:]
    )
    _global_override_seconds = args.timeout_seconds if _explicit_timeout_override else None

    def _emit_blocked_policy_error(
        _error_code: str,
        _rule: str,
        _message: str,
        _minimal_context: str,
        _fix_hint: str,
    ) -> int:
        _policy_error = {
            "kind": _error_code,
            "rule": _rule,
            "message": _message,
            "minimal_context": _minimal_context,
            "fix_hint": _fix_hint,
        }
        _policy_result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": 0,
            },
            "results": [],
            "errors": [_policy_error],
            "failure_class": _error_code,
            "retryable": False,
            "diagnostic_report": not_computed_diagnostic_report(),
        }
        emit_json(_policy_result, args.evidence_mode, current_evidence)
        return exit_code_for_status("blocked")

    # Issue #2254 AC1: this is the ONE root-owned read of the local
    # history store for this invocation's plan (skipped entirely under
    # --no-history-estimator, and loaded verbatim from
    # --history-snapshot-file when a parent process already produced it --
    # both keep plan_digest convergent with whatever the caller/child
    # agreed on).
    # Issue #2254 fix_delta P0 blocker 2/3 (OWNER REQUEST_CHANGES
    # source-repo PR 2382 review comment 5458281756):
    # `_repo_root_for_history` is resolved EXACTLY ONCE from `args.cwd` and
    # reused for BOTH producing/loading the snapshot AND computing the
    # plan below -- a caller/parent process resolving `repo_root` from the
    # SAME `--cwd` value independently converges on the SAME string (it is
    # a pure function of `cwd`), so `command_group_key` stays convergent
    # across this subprocess boundary without any extra handoff.
    _history_snapshot: Optional[Dict[str, Any]] = None
    _repo_root_for_history: Optional[str] = None
    if not args.no_history_estimator:
        if args.history_snapshot_file:
            _repo_root_for_history = resolve_repo_root_for_history(args.cwd)
            _history_snapshot = _vc_runtime_history.load_history_snapshot_file(
                Path(args.history_snapshot_file)
            )
        elif not (
            os.environ.get("PYTEST_CURRENT_TEST")
            and not os.environ.get("VC_RUNTIME_HISTORY_STORE_PATH")
        ):
            # This mirrors produce_immutable_history_snapshot()'s OWN
            # internal test-safety guard (see its docstring) -- resolving
            # `repo_root` here shells out to a REAL `git` subprocess, which
            # must be skipped in the SAME cases that guard already skips,
            # not merely inside that function, so tests that monkeypatch
            # this module's OWN `subprocess.Popen` for unrelated
            # VC-execution assertions never observe an unexpected extra
            # `git rev-parse --show-toplevel` call.
            _repo_root_for_history = resolve_repo_root_for_history(args.cwd)
            _history_snapshot = produce_immutable_history_snapshot(
                body, cwd=args.cwd, repo_root=_repo_root_for_history
            )
        # else: pytest test-safety guard -- `_history_snapshot` /
        # `_repo_root_for_history` stay None (never read downstream: a
        # `None` `_history_snapshot` short-circuits every
        # `repo_root`-dependent lookup in `compute_command_timeout_budget()`).

    try:
        _vc_plan = compute_canonical_vc_plan(
            body,
            global_override_seconds=_global_override_seconds,
            cwd=args.cwd,
            allowed_paths=allowed_paths_from_body,
            history_snapshot=_history_snapshot,
            repo_root=_repo_root_for_history,
        )
    except CommandTimeoutExceedsPolicyError as _budget_exc:
        return _emit_blocked_policy_error(
            "command_timeout_exceeds_policy",
            "VCP_COMMAND_TIMEOUT_EXCEEDS_POLICY",
            str(_budget_exc),
            f"command_hash=sha256:{_budget_exc.command_hash}",
            (
                "Requested per-command timeout exceeds the fixed policy ceiling "
                f"({_budget_exc.max_seconds}s); no subprocess was launched."
            ),
        )
    except CommandTimeoutNonPositiveError as _nonpos_exc:
        return _emit_blocked_policy_error(
            "command_timeout_non_positive",
            "VCP_COMMAND_TIMEOUT_NON_POSITIVE",
            str(_nonpos_exc),
            f"command_hash=sha256:{_nonpos_exc.command_hash}",
            "Resolved per-command timeout must be > 0 seconds; no subprocess was launched.",
        )
    except AggregateTimeoutExceedsPolicyError as _agg_exc:
        return _emit_blocked_policy_error(
            "aggregate_timeout_exceeds_policy",
            "VCP_AGGREGATE_TIMEOUT_EXCEEDS_POLICY",
            str(_agg_exc),
            f"aggregate_timeout_seconds={_agg_exc.aggregate_timeout_seconds}",
            (
                "Aggregate command-level timeout budget exceeds the fixed policy "
                f"ceiling ({_agg_exc.max_seconds}s); no subprocess was launched."
            ),
        )

    try:
        verify_canonical_vc_plan_digest(_vc_plan, args.expected_plan_digest)
    except VcPlanDigestMismatchError as _digest_exc:
        return _emit_blocked_policy_error(
            "vc_plan_digest_mismatch",
            "VCP_PLAN_DIGEST_MISMATCH",
            str(_digest_exc),
            f"expected={_digest_exc.expected_digest}",
            (
                "The canonical VC plan recomputed from this process's own body "
                "does not match --expected-plan-digest supplied by the caller; "
                "no subprocess was launched."
            ),
        )

    # Issue #2232: duplicate non-pure VC command diagnostic, independent of
    # the canonical execution plan above (never influences status/results/
    # exit code -- see the "Duplicate non-pure VC command diagnostics"
    # section docstring near compute_duplicate_diagnostic_report()).
    _diagnostic_report = compute_duplicate_diagnostic_report(
        body,
        _vc_plan["plan_digest"],
        args.cwd,
        allowed_paths_from_body,
    )

    _command_budgets_by_hash: Dict[str, Dict[str, Any]] = {
        _b["command_hash"]: _b for _b in _vc_plan["command_budgets"]
    }

    def _timeout_provenance_for(command_text: str) -> Dict[str, Any]:
        _b = _command_budgets_by_hash[compute_command_hash(command_text)]
        return {
            "timeout_seconds": _b["timeout_seconds"],
            "cleanup_tail_seconds": _b["cleanup_tail_seconds"],
            "source": _b["source"],
            "estimator_version": _b["estimator_version"],
            "estimator_input_digest": _b["estimator_input_digest"],
        }

    def _effective_timeout_for(command_text: str) -> int:
        return _command_budgets_by_hash[compute_command_hash(command_text)]["timeout_seconds"]

    # ---------------------------------------------------------------------
    # Issue #1338 (PR #1508 review P0-1/P0-2/P1-1 fixes)
    #
    # State-epoch-aware execution cache + contiguous pure-observation
    # batching, executed INLINE (in Issue-body order) rather than deferred
    # to a single end-of-loop phase. This fixes two review findings:
    #
    #   P0-1: execution_key_hash previously covered only argv/cwd/env/
    #   timeout, so two textually-identical pure observations that bracket
    #   a stateful command (e.g. `test -d X` .. `python3 -m py_compile Y` ..
    #   `test -d X`) would incorrectly dedup-replay the second against the
    #   first's stale pre-mutation snapshot. `_state_epoch` now advances
    #   every time a non-pure ("barrier") command executes, and is folded
    #   into compute_execution_key_hash(), so the two `test -d X` calls above
    #   get DIFFERENT keys and are never conflated.
    #
    #   P1-1: the previous design deferred ALL "safe to run" commands
    #   (including pnpm/pytest/git) to a single batch phase after the whole
    #   Issue body had been parsed, which could reorder their actual
    #   subprocess launches relative to commands executed immediately in
    #   this same loop (e.g. github_metadata_assert, a live `gh api` call).
    #   Executing inline, right here, preserves the exact Issue-body
    #   execution order for every side-effecting/observing command.
    #
    # Only a CONTIGUOUS run of pure-observation commands (exact
    # `test -f|-d|-s PATH`, or a fully validated `rg` -- see
    # _is_parallel_eligible_command) is ever batched for concurrent
    # execution (only when --max-workers > 1); a barrier command always
    # flushes that batch first, so parallel batching never reorders a
    # barrier relative to its neighbors.
    # ---------------------------------------------------------------------
    _state_epoch = [0]  # boxed int so nested closures can mutate it
    _exec_cache: Dict[str, Dict[str, Any]] = {}
    _cache_meta: Dict[str, Dict[str, Any]] = {}
    _command_hash_by_key: Dict[str, str] = {}
    _parallel_batch: List[Dict[str, Any]] = []
    # Keys that have already been consumed by a FINALIZED (result-emitting)
    # job. Distinct from `_exec_cache` (which may already hold an entry for
    # a key purely because THIS SAME job's own subprocess just populated it
    # -- e.g. inside a freshly-launched parallel batch); only a key that was
    # already in `_dedup_seen_keys` at finalize-time is a true dedup replay.
    _dedup_seen_keys: set = set()

    def _run_and_cache_pure_job(job: Dict[str, Any]) -> None:
        _key = job["_key"]
        _exit_code, _stdout, _stderr, _duration_ms, _runner_env_delta = run_command(
            job["command"], _effective_timeout_for(job["command"]), args.cwd
        )
        # Issue #2254 AC6/AC7: this run_command() call IS the one real
        # subprocess launch for this (dedup-cacheable) key -- record it
        # BEFORE the P0-2 quiet-success mutation below, which is a VC
        # CLASSIFICATION concern, not a process-completion concern.
        if not args.no_history_estimator:
            record_command_execution_sample(
                job["command"],
                args.cwd,
                exit_code=_exit_code,
                stderr=_stderr,
                duration_ms=_duration_ms,
                applied_timeout_seconds=_effective_timeout_for(job["command"]),
                repo_root=_repo_root_for_history,
            )
        # P0-2: fail-closed quiet-success -- a pure observation VC that exits
        # 0 but leaves non-empty stderr is not a trustworthy silent pass.
        if _exit_code == 0 and _stderr.strip():
            _exit_code = 1
        _exec_cache[_key] = {
            "exit_code": _exit_code,
            "stdout": _stdout,
            "stderr": _stderr,
            "duration_ms": _duration_ms,
            "runner_env_delta": _runner_env_delta,
        }
        _cache_meta[_key] = {"idx": job["idx"], "ac": job["ac_label"], "line": job["line_no"]}
        _command_hash_by_key[_key] = f"sha256:{compute_command_hash(job['command'])}"

    def _finalize_and_store_job(job: Dict[str, Any]) -> None:
        _key = compute_execution_key_hash(
            job["argv"], args.cwd, job["env_delta"], args.timeout_seconds,
            state_epoch=_state_epoch[0],
        )
        _is_replay = job["is_pure"] and _key in _dedup_seen_keys
        if _is_replay:
            _outcome = _exec_cache[_key]
        elif job["is_pure"]:
            if _key not in _exec_cache:
                _run_and_cache_pure_job({**job, "_key": _key})
            _outcome = _exec_cache[_key]
            _dedup_seen_keys.add(_key)
        else:
            _exit_code, _stdout, _stderr, _duration_ms, _runner_env_delta = run_command(
                job["command"], _effective_timeout_for(job["command"]), args.cwd
            )
            # Issue #2254 AC6/AC7: a non-pure job is NEVER dedup-replayed --
            # every call here is its own real subprocess launch.
            if not args.no_history_estimator:
                record_command_execution_sample(
                    job["command"],
                    args.cwd,
                    exit_code=_exit_code,
                    stderr=_stderr,
                    duration_ms=_duration_ms,
                    applied_timeout_seconds=_effective_timeout_for(job["command"]),
                    repo_root=_repo_root_for_history,
                )
            _outcome = {
                "exit_code": _exit_code,
                "stdout": _stdout,
                "stderr": _stderr,
                "duration_ms": _duration_ms,
                "runner_env_delta": _runner_env_delta,
            }
            # P0-1: this is a state barrier -- advance the epoch so later
            # pure observations of identical argv/cwd/env never dedup
            # against a stale pre-barrier snapshot.
            _state_epoch[0] += 1

        _classification, _category, _decision, _fix_hint, _scope_class = classify_result(
            _outcome["exit_code"],
            _outcome["stdout"],
            _outcome["stderr"],
            job["command"],
            cwd=args.cwd,
            runner_env_delta=_outcome["runner_env_delta"],
            allowed_paths=allowed_paths_from_body,
            static_policy_passed=True,
            evidence_mode=args.evidence_mode,
        )

        _job_baseline_expect = job["baseline_expect"]
        if _job_baseline_expect == "pass":
            if _outcome["exit_code"] == 0 and _classification in ("unexpected_pass", "expected_pass"):
                _classification = "expected_pass"
                _category = "baseline_expect_pass"
                _decision = "go"
                _scope_class = "regression_gate"
                _fix_hint = None
            elif _outcome["exit_code"] is not None and _outcome["exit_code"] != 0:
                # Issue #2638: `vc_grep_syntax_error` is exempted from this
                # `baseline-expect: pass` override for the SAME reason
                # `package_manager_no_tty_prompt` already is -- it is a
                # deterministically recognized VC-design defect (rg/grep
                # regex-syntax / invalid-option / usage error, exit code 2),
                # not a genuine baseline regression. Without this exemption,
                # `classify_result()`'s new category would be silently
                # overwritten back to `human_judgment` / `baseline_regression_failed`
                # here, defeating the whole point of the new category (OWNER
                # REQUEST_CHANGES P1-1). This exemption is intentionally
                # scoped to ONLY these two categories; every other genuine
                # baseline regression (env_missing_dep, timeout, etc.) keeps
                # the existing override behavior unchanged.
                if _category in ("package_manager_no_tty_prompt", VC_GREP_SYNTAX_ERROR_CATEGORY):
                    pass
                else:
                    _classification = "human_judgment"
                    _category = "baseline_regression_failed"
                    _decision = "human_judgment"
                    _fix_hint = (
                        "VC annotated baseline-expect: pass but exited non-0; "
                        "the command that was passing at baseline is now failing. "
                        "Investigate regression or update annotation."
                    )
        elif _job_baseline_expect == "fail":
            # Issue #2515: an explicit author-declared `# baseline-expect: fail`
            # is a deterministic override for the `existing_file_missing_node_id_
            # noncanonical` heuristic (Issue #1285/#1347/PR #1305). That heuristic
            # exists to catch a VC that AMBIGUOUSLY references a not-yet-existing
            # node-id (function or class) inside an EXISTING file -- it cannot
            # tell a genuine typo/wrong-target VC apart from an intentional
            # "this class/function is being added to this existing allowed file"
            # VC. When the author has explicitly annotated the VC with
            # `# baseline-expect: fail`, that ambiguity is resolved by the
            # author's own declaration, so the ordinary `expected_baseline_fail`
            # path applies instead of the non-canonical-shape block. This does
            # NOT distinguish function vs. class node-ids (neither did the
            # original heuristic) and does NOT change behavior for any other
            # `category` -- only `existing_file_missing_node_id_noncanonical`
            # with `decision == "blocked"` is affected, and only when the
            # annotation is present.
            if (
                _category == "existing_file_missing_node_id_noncanonical"
                and _decision == "blocked"
            ):
                _classification = "expected_fail"
                _category = "existing_file_missing_node_id_declared_fail"
                _decision = "go"
                _scope_class = "baseline_fail_expected"
                _fix_hint = None
        else:
            if _classification == "unexpected_pass" and _decision == "blocked":
                _existing_hint = _fix_hint or ""
                _missing_hint = (
                    " [missing_annotation] Consider adding "
                    "# baseline-expect: pass on the preceding line "
                    "if this VC is expected to pass at baseline "
                    "(e.g., for a promotion/refactor Issue)."
                )
                _fix_hint = _existing_hint + _missing_hint

        _source_meta = _cache_meta.get(_key) if _is_replay else None
        _result_item = _build_result_item(
            ac_label=job["ac_label"],
            line_no=job["line_no"],
            command=job["command"],
            exit_code=_outcome["exit_code"],
            classification=_classification,
            category=_category,
            decision=_decision,
            scope_class=_scope_class,
            stdout=_outcome["stdout"],
            stderr=_outcome["stderr"],
            duration_ms=_outcome["duration_ms"],
            fix_hint=_fix_hint,
            runner_env_delta=_outcome["runner_env_delta"],
            preflight_scope=job["preflight_scope"],
            baseline_expect=_job_baseline_expect,
            vc_role=job["vc_role"],
            annotation_line_no=job["annotation_line_no"],
            annotation_raw=job["annotation_raw"],
            verification_owner=None,
            deferred_reason=None,
            runtime_verification_required=None,
            strict_enabled=args.strict,
            max_head_lines=args.max_head_lines,
            execution_key_hash=_key,
            is_dedup_replay=_is_replay,
            source_command_hash=(_command_hash_by_key.get(_key) if _is_replay else None),
            source_execution_key_hash=(_key if _is_replay else None),
            source_result_index=(_source_meta["idx"] if _source_meta else None),
            source_ac=(_source_meta["ac"] if _source_meta else None),
            source_line=(_source_meta["line"] if _source_meta else None),
            certified_target_paths=(
                _extract_certified_target_paths(job["command"])
                if _category == "expected_pass_resolved_on_current_head"
                else None
            ),
            cwd=args.cwd,
            timeout_provenance=_timeout_provenance_for(job["command"]),
        )
        results[job["idx"]] = _result_item
        summary[_result_item["classification"]] += 1

    def _flush_parallel_batch() -> None:
        """Execute (and finalize) every job buffered in `_parallel_batch`,
        preserving the AC5 contract: with --max-workers > 1, distinct
        not-yet-cached execution keys in this contiguous batch are launched
        concurrently through a single ThreadPoolExecutor; every job (cached,
        deduped, or freshly launched) is then finalized in original order."""
        if not _parallel_batch:
            return
        _batch = list(_parallel_batch)
        _parallel_batch.clear()
        if args.max_workers > 1:
            _launch_order: List[str] = []
            _launch_jobs: Dict[str, Dict[str, Any]] = {}
            for _job in _batch:
                _key = compute_execution_key_hash(
                    _job["argv"], args.cwd, _job["env_delta"], args.timeout_seconds,
                    state_epoch=_state_epoch[0],
                )
                if _key in _exec_cache or _key in _launch_jobs:
                    continue
                _job_with_key = dict(_job)
                _job_with_key["_key"] = _key
                _launch_jobs[_key] = _job_with_key
                _launch_order.append(_key)
            if _launch_jobs:
                with ThreadPoolExecutor(max_workers=args.max_workers) as _executor:
                    _future_to_key = {
                        _executor.submit(_run_and_cache_pure_job, _launch_jobs[_key]): _key
                        for _key in _launch_order
                    }
                    for _future in as_completed(_future_to_key):
                        _future.result()
        for _job in _batch:
            _finalize_and_store_job(_job)

    for _cmd_idx, (
        ac_label, command, line_no, preflight_scope, vc_regex_intent,
        baseline_expect, vc_role, annotation_line_no, annotation_raw,
    ) in enumerate(commands):
        runner_env_delta: Dict[str, str] = {}
        # AC5: Handle pr_review_only / runtime_only preflight-scope markers
        # NB2: Invalid marker values (typos) → human_judgment
        if preflight_scope is not None:
            if preflight_scope in VALID_PRE_FLIGHT_SCOPE_VALUES:
                classification = "skipped"
                decision = "go"
                category = f"preflight_scope_{preflight_scope}"
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                fix_hint = None
                scope_class = preflight_scope
                # Routing metadata for skipped results
                verification_owner = "pr-review-judge" if preflight_scope == "pr_review_only" else "impl-review-loop"
                deferred_reason = (
                    "VC marked pr_review_only; verification deferred to PR review"
                    if preflight_scope == "pr_review_only"
                    else "VC marked runtime_only; verification deferred to post-implementation runtime"
                )
                runtime_verification_required = preflight_scope == "runtime_only"
            else:
                # NB2: Invalid preflight-scope marker value
                classification = "human_judgment"
                decision = "human_judgment"
                category = "unknown"
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                fix_hint = (
                    f"Unknown preflight-scope marker value '{preflight_scope}';"
                    " expected pr_review_only or runtime_only"
                )
                scope_class = "baseline_fail_expected"
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None

        else:
            # AC2/AC12: inline '# baseline-expect:' is an invalid placement — detect
            # BEFORE any execution so the malformed command is never run.
            if has_unquoted_inline_baseline_expect(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification = "blocked"
                category = "inline_baseline_expect_invalid_placement"
                decision = "blocked"
                fix_hint = (
                    "Inline '# baseline-expect:' alters command semantics and is not "
                    "executed; move the annotation to the immediately preceding comment line."
                )
                scope_class = "baseline_fail_expected"
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None
            # BLOCKER 2 fix: invalid baseline-expect annotation value → human_judgment
            # Sentinel "__invalid__:<raw>" is set by extract_baseline_expect_annotation
            # when the annotation line is present but value is not in VALID_BASELINE_EXPECT_VALUES.
            elif baseline_expect is not None and baseline_expect.startswith("__invalid__:"):
                raw_invalid_value = baseline_expect[len("__invalid__:"):]
                classification = "human_judgment"
                decision = "human_judgment"
                category = "invalid_baseline_expect_annotation"
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                scope_class = "baseline_fail_expected"
                fix_hint = (
                    f"# baseline-expect: {raw_invalid_value} is not a valid annotation. "
                    "Use pass|fail|deferred."
                )
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None

            # Issue #889: baseline-expect: deferred → skip (priority after preflight-scope)
            elif baseline_expect == "deferred":
                classification = "skipped"
                decision = "go"
                category = "baseline_expect_deferred"  # MAJOR 3 fix: distinct category
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                fix_hint = None
                scope_class = "pr_review_only"
                verification_owner = "pr-review-judge"
                deferred_reason = "VC annotated baseline-expect: deferred; verification deferred"
                runtime_verification_required = False

            # Static classification checks BEFORE run_command (CRITICAL)
            # Negated search commands are statically classified as expected_fail/go
            elif _is_negated_search_command(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification, category, decision, fix_hint, scope_class = (
                    "expected_fail",
                    "expected_baseline_fail",
                    "go",
                    None,
                    "baseline_fail_expected",
                )
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None
            elif (
                args.strict
                and baseline_expect is None
                and classify_static_command(command, Path(args.cwd), allowed_paths=allowed_paths_from_body) is None
                and _candidate_new_allowed_path_target(command, allowed_paths_from_body, args.cwd) is not None
            ):
                # AC4/AC8/AC14: strict mode — VC targets a NEW Allowed Path file that
                # does not exist at baseline and lacks a baseline-expect annotation.
                _missing_target = _candidate_new_allowed_path_target(
                    command, allowed_paths_from_body, args.cwd
                )
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification = "blocked"
                category = "missing_baseline_expect_for_new_allowed_path"
                decision = "blocked"
                fix_hint = (
                    f"VC targets new Allowed Path '{_missing_target}' which does not exist at "
                    "baseline; add '# baseline-expect: fail' on the preceding line."
                )
                scope_class = "baseline_fail_expected"
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None
            else:
                # AC1-AC3: classify_static_command checks unsafe/unsupported commands
                # BEFORE any execution attempt
                static_result = classify_static_command(
                    command, Path(args.cwd), allowed_paths=allowed_paths_from_body
                )
                # AC3 (Issue #589): If regex_literal_pipe_suspected and literal-pipe-ok
                # annotation is present, skip the blocked classification and proceed to execute.
                if (
                    static_result is not None
                    and static_result[1] == "regex_literal_pipe_suspected"
                    and vc_regex_intent == "literal-pipe-ok"
                ):
                    static_result = None  # annotation exempts from blocked
                if static_result is not None:
                    # no_override_for_blocker: static_blocker takes precedence — baseline-expect does NOT override
                    # static blocks
                    exit_code, stdout, stderr, duration_ms = None, "", "", 0
                    classification, category, decision, fix_hint, scope_class = static_result
                elif _is_github_metadata_assert_command(command):
                    # Issue #942: github_metadata_assert is allowed (static_result is None) but
                    # is NOT a real executable binary. Instead of run_command (which would hit
                    # "No such file or directory"), dispatch the assertion to
                    # _check_github_metadata_assertion and classify by its exit code.
                    # subprocess is invoked there with a fixed read-only argv (gh api --method GET).
                    # PR #1508 review (P1-1): flush any buffered pure-observation
                    # parallel batch first, so this live `gh api` call keeps its
                    # exact Issue-body relative order against the VCs around it.
                    _flush_parallel_batch()
                    try:
                        _assert_argv = shlex.split(command, posix=True)
                    except ValueError:
                        _assert_argv = []
                    # static_result is None implies _is_allowed_github_metadata_assert validated argv,
                    # so positions 1..4 are present and safe to read.
                    _assertion_type = _assert_argv[1]
                    _field = _assert_argv[2]
                    _literal = _assert_argv[3]
                    _endpoint = _assert_argv[4]
                    assert_exit = _check_github_metadata_assertion(
                        _assertion_type, _field, _literal, _endpoint,
                        timeout_seconds=_effective_timeout_for(command),
                    )
                    # P0-1: an external GitHub API observation is a state
                    # barrier -- advance the epoch so later pure observations
                    # are never dedup-replayed across it.
                    _state_epoch[0] += 1
                    exit_code, stdout, stderr, duration_ms = assert_exit, "", "", 0
                    if assert_exit == 0:
                        # assertion holds (present for contains / absent for not_contains)
                        classification = "expected_pass"
                        category = "github_metadata_assert_pass"
                        decision = "go"
                        scope_class = "regression_gate"
                        fix_hint = None
                    elif assert_exit == 1:
                        # assertion does not hold; same go disposition as other expected_fail VCs
                        classification = "expected_fail"
                        category = "github_metadata_assert_fail"
                        decision = "go"
                        scope_class = "baseline_fail_expected"
                        fix_hint = None
                    else:
                        # exit 2..8: environment error (gh missing / auth / 404 / rate limit /
                        # timeout / invalid JSON / other HTTP). MUST NOT be a false pass (go).
                        classification = "human_judgment"
                        category = "github_metadata_assert_environment_error"
                        decision = "human_judgment"
                        scope_class = "baseline_fail_expected"
                        fix_hint = (
                            "github_metadata_assert hit an environment error "
                            f"(exit {assert_exit}: gh missing / auth / 404 / rate limit / "
                            "timeout / invalid JSON / missing field). This is distinct from assertion "
                            "pass/fail and is not treated as a baseline pass."
                        )
                else:
                    # Issue #1338 (PR #1508 review P0-1/P1-1): execute inline,
                    # in Issue-body order, through the state-epoch-aware
                    # execution cache / contiguous parallel batch defined
                    # above `for _cmd_idx, (...)`. classify_result() and the
                    # baseline-expect re-map are always recomputed from THIS
                    # AC's own command/annotations (never copied from another
                    # AC's classification/decision) inside
                    # _finalize_and_store_job().
                    try:
                        _this_argv = shlex.split(command)
                    except ValueError:
                        _this_argv = []
                    _this_env_delta = _fixed_env_delta_for_argv(_this_argv)
                    _this_is_pure = _is_parallel_eligible_command(
                        command, args.cwd, allowed_paths_from_body
                    )
                    _job = {
                        "idx": _cmd_idx,
                        "ac_label": ac_label,
                        "line_no": line_no,
                        "command": command,
                        "baseline_expect": baseline_expect,
                        "vc_role": vc_role,
                        "annotation_line_no": annotation_line_no,
                        "annotation_raw": annotation_raw,
                        "preflight_scope": preflight_scope,
                        "argv": _this_argv,
                        "env_delta": _this_env_delta,
                        "is_pure": _this_is_pure,
                    }
                    if _this_is_pure and args.max_workers > 1:
                        # AC5/P1-1: only buffer a CONTIGUOUS run of pure
                        # observation VCs; any barrier command flushes this
                        # batch (see the `else` branch just below) before it
                        # executes, so relative order across a barrier is
                        # never disturbed.
                        _parallel_batch.append(_job)
                        verification_owner = None
                        deferred_reason = None
                        runtime_verification_required = None
                        continue
                    _flush_parallel_batch()
                    _finalize_and_store_job(_job)
                    verification_owner = None
                    deferred_reason = None
                    runtime_verification_required = None
                    continue

                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None

        # Issue #1338: result-shaping is shared with the deferred (pending-job)
        # finalization phase below via _build_result_item(), so both the
        # immediate and dedup/parallel-execution code paths produce identical
        # entry shapes.
        result_item = _build_result_item(
            ac_label=ac_label,
            line_no=line_no,
            command=command,
            exit_code=exit_code,
            classification=classification,
            category=category,
            decision=decision,
            scope_class=scope_class,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            fix_hint=fix_hint,
            runner_env_delta=runner_env_delta,
            preflight_scope=preflight_scope,
            baseline_expect=baseline_expect,
            vc_role=vc_role,
            annotation_line_no=annotation_line_no,
            annotation_raw=annotation_raw,
            verification_owner=verification_owner,
            deferred_reason=deferred_reason,
            runtime_verification_required=runtime_verification_required,
            strict_enabled=args.strict,
            max_head_lines=args.max_head_lines,
            certified_target_paths=(
                _extract_certified_target_paths(command)
                if category == "expected_pass_resolved_on_current_head"
                else None
            ),
            cwd=args.cwd,
            timeout_provenance=_timeout_provenance_for(command),
        )
        results[_cmd_idx] = result_item
        summary[result_item["classification"]] += 1

    # Issue #1338 (PR #1508 review P1-1): flush any pure-observation batch
    # still buffered after the last Issue-body command (e.g. the body ends
    # with a contiguous run of `rg`/`test` VCs with no trailing barrier).
    _flush_parallel_batch()

    # B2: status 優先順位を blocked > human_judgment > pass にする
    status = "pass"
    if not results:
        status = "blocked"
    elif any(r["decision"] == "blocked" for r in results):
        status = "blocked"
    elif any(r["decision"] == "human_judgment" for r in results):
        status = "human_judgment"

    output = {
        "schema": "baseline_vc_preflight/v1",
        "issue": args.issue or 0,
        "repo": args.repo,
        "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "source": {
            "kind": source_kind,
            "body_sha256": f"sha256:{compute_source_hash(body)}",
        },
        "status": status,
        "summary": summary,
        "results": results,
        "errors": [],
        "diagnostic_report": _diagnostic_report,
    }

    if current_evidence is not None:
        current_evidence = finalize_current_head_evidence(args.cwd, current_evidence)
        if not current_evidence["certified"]:
            output["status"] = "blocked"
            output["errors"].extend(current_evidence["errors"])

    # B8: Output format selection
    if args.format == "contract-review-fragment":
        # C1: lazy import yaml - only when needed for fragment output
        try:
            import yaml
        except ImportError:
            error_result = {
                "schema": "baseline_vc_preflight/v1",
                "issue": args.issue or 0,
                "repo": args.repo,
                "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                "source": {
                    "kind": source_kind,
                    "body_sha256": f"sha256:{compute_source_hash(body)}",
                },
                "status": "blocked",
                "summary": summary,
                "results": results,
                "errors": ["PyYAML is required for contract-review-fragment format; install pyyaml"],
                "diagnostic_report": _diagnostic_report,
            }
            emit_json(error_result, args.evidence_mode, current_evidence)
            # C2: exit code 2 for missing dependency
            return 2
        fragment = generate_contract_review_fragment(status, results)
        print(yaml.dump(fragment, default_flow_style=False))
    else:
        emit_json(output, args.evidence_mode, current_evidence)

    # C2: Exit code contract
    # status: pass → 0, blocked → 1, human_judgment → 3
    return exit_code_for_status(output["status"])


_TEST_REAP_GRACE_SECONDS_ENV_VAR = "BASELINE_VC_PREFLIGHT_TEST_REAP_GRACE_SECONDS"
_TEST_SIGTERM_MARKER_PATH_ENV_VAR = "BASELINE_VC_PREFLIGHT_TEST_SIGTERM_MARKER_PATH"


def _test_reap_grace_seconds_override() -> Optional[float]:
    """Issue #2207 OWNER P1-2 item 5: test-only dependency injection so a
    scaled fault-injection test can bound `reap_all_active_process_groups()`'s
    grace period to a fast value instead of the production 5.0s default --
    without this, a test that spawns a SIGTERM-ignoring grandchild would
    have to wait the full production grace window on every repeat. Absent
    or unparseable in production (the env var is never set by any
    production caller), this returns `None` and production behavior
    (5.0s default) is unchanged."""
    raw = os.environ.get(_TEST_REAP_GRACE_SECONDS_ENV_VAR)
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def main() -> int:
    """Issue #2207 OWNER P0-2: the ONLY place `CooperativeCancellationRequested`
    is caught -- a single top-level handler that runs the ACTUAL registry
    snapshot + TERM/KILL + wait work in ordinary (non-signal-handler)
    control flow, then terminates this process via the default SIGTERM
    disposition (so the process's own exit still looks like a normal
    signal-based termination to whatever supervised it)."""
    try:
        return _main_impl()
    except CooperativeCancellationRequested:
        # Issue #2207 OWNER P1-2 item 5: test-only marker write, proving the
        # SIGTERM handler actually ran (as opposed to this process being
        # killed outright by an outer supervisor without ever reaching this
        # handler). Best-effort and a pure no-op unless a test sets the env
        # var; never raises into the reap/terminate path below.
        _marker_path = os.environ.get(_TEST_SIGTERM_MARKER_PATH_ENV_VAR)
        if _marker_path:
            try:
                with open(_marker_path, "w", encoding="utf-8") as _mf:
                    _mf.write(f"sigterm_handler_entered pid={os.getpid()}\n")
            except OSError:
                pass
        _grace = _test_reap_grace_seconds_override()
        if _grace is not None:
            reap_all_active_process_groups(grace_seconds=_grace)
        else:
            reap_all_active_process_groups()
        sys.stderr.write(
            "baseline_vc_preflight: SIGTERM received, reaped active VC process groups\n"
        )
        sys.stderr.flush()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
        # Unreachable in practice (the line above re-delivers SIGTERM to
        # this process under its default disposition, which terminates
        # it); kept only so a non-POSIX/mocked test double that stubs
        # `os.kill` without actually terminating the process still returns
        # a deterministic non-zero exit code instead of falling through to
        # an implicit `None`.
        return 143


if __name__ == "__main__":
    sys.exit(main())

